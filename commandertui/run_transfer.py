"""Textual orchestration for the shared copy/move transfer engine."""

from __future__ import annotations

import os
import time
from dataclasses import replace

from .conflicts import Resolution, detect_conflicts
from .executor import TransferStatus
from .modals import ConfirmScreen, ConflictScreen, MessageScreen
from .models import OpKind, OpSide, OperationQueue, QueuedOp
from .progress_screen import ProgressScreen
from .scanner import tree_stats
from .transfer import ConflictPolicy, build_transfer_plan, execute_transfer_plan

CANCELLED_TEXT = (
    "Cancelled.\n\nFiles already copied stay at the destination.\n"
    "The file in progress was discarded - no half-copied files were left.\n"
    "For a move, the source of anything unfinished is still in place."
)


def run_transfer(owner, kind, label, src_panel, dst_panel, sources):
    destination = dst_panel.path
    scanning = ProgressScreen("Checking copy/move conflicts…", len(sources))
    owner.app.push_screen(scanning)

    def scanned(report, queue, error=None):
        scanning.dismiss()
        if error:
            owner.app.push_screen(MessageScreen(f"Could not scan sources: {error}"))
        elif scanning.cancel_requested:
            owner.app.push_screen(MessageScreen(CANCELLED_TEXT))
        elif report.has_conflicts or report.dir_collisions:
            choose_policy(report)
        else:
            owner.app.push_screen(
                ConfirmScreen(f"{label} to {destination}", queue),
                lambda confirmed: (
                    execute(report, ConflictPolicy(Resolution.OVERWRITE)) if confirmed else None
                ),
            )

    def scan():
        try:
            report = detect_conflicts(sources, destination, lambda: scanning.cancel_requested)
            queue = OperationQueue()
            side = OpSide.LEFT_TO_RIGHT if src_panel.side == "left" else OpSide.RIGHT_TO_LEFT
            for source in sources:
                _, _, size = tree_stats([source], lambda: scanning.cancel_requested)
                queue.add(
                    QueuedOp(
                        kind, side, os.path.relpath(source, src_panel.path), os.path.isdir(source), size=size
                    )
                )
            owner.app.call_from_thread(scanned, report, queue)
        except Exception as error:
            owner.app.call_from_thread(scanned, None, None, str(error))

    def choose_policy(report):
        def selected(answer):
            value, _ = answer
            if value == "cancel":
                return
            if value == "ask":
                ask_each(report)
            elif value == "merge":

                def merged(answer):
                    value, _ = answer
                    if value != "cancel":
                        execute(
                            report,
                            ConflictPolicy(
                                Resolution(value), {path: Resolution.MERGE for path in report.dir_collisions}
                            ),
                        )

                owner.app.push_screen(ConflictScreen(report, destination, kind, file_policy=True), merged)
            else:
                execute(report, ConflictPolicy(Resolution(value)))

        owner.app.push_screen(ConflictScreen(report, destination, kind), selected)

    def ask_each(report):
        # A rel_path is the policy key, so homonymous source names share a
        # decision, displayed explicitly in the summary.
        paths = list(dict.fromkeys([c.rel_path for c in report.conflicts] + report.duplicate_sources))
        paths += [p for p in report.dir_collisions if not any(x == p or x.startswith(p + "/") for x in paths)]
        policy = ConflictPolicy(Resolution.SKIP)

        def next_item(index):
            if index == len(paths):
                execute(report, policy)
                return

            def selected(answer):
                value, apply = answer
                if value == "cancel":
                    return  # Nothing has executed yet, including prior decisions.
                remaining = paths[index:] if apply else [paths[index]]
                for path in remaining:
                    policy.overrides[path] = Resolution(value)
                next_item(len(paths) if apply else index + 1)

            owner.app.push_screen(ConflictScreen(report, destination, kind, rel_path=paths[index]), selected)

        next_item(0)

    def execute(report, policy):
        progress = ProgressScreen(f"{label} to {destination}", len(sources))
        owner.app.push_screen(progress)

        def work():
            try:
                plan = build_transfer_plan(sources, destination, kind, report, policy)
                status = TransferStatus(ops_total=len(plan.items))
                for item in plan.items:
                    if item.action != "skip":
                        files, _, size = tree_stats([item.src_abs], lambda: progress.cancel_requested)
                        status.files_total += files
                        status.bytes_total += size
                last = 0.0
                current_done = 0
                completed = set()
                previous_failures = 0

                def update(done, total, name):
                    nonlocal last, current_done
                    if done == 0:
                        current_done = 0
                        status.files_done += 1
                    status.bytes_done += done - current_done
                    current_done = done
                    status.file_name, status.file_done, status.file_total = name, done, total
                    now = time.monotonic()
                    if now - last >= 0.1 or done == total:
                        last = now
                        owner.app.call_from_thread(progress.report, replace(status))

                def on_item(item, result):
                    nonlocal previous_failures
                    status.ops_done += 1
                    if (
                        item.action != "skip"
                        and not result.cancelled
                        and len(result.failed) == previous_failures
                    ):
                        completed.add(item.src_abs)
                    previous_failures = len(result.failed)
                    owner.app.call_from_thread(progress.report, replace(status))

                result = execute_transfer_plan(plan, kind, update, lambda: progress.cancel_requested, on_item)
                owner.app.call_from_thread(finish, plan, result, completed)
            except Exception as error:
                owner.app.call_from_thread(failed, str(error))

        def failed(error):
            progress.dismiss()
            owner.app.push_screen(MessageScreen(f"Could not transfer: {error}"))

        def finish(plan, result, completed):
            progress.dismiss()
            for source in sources:
                related = [
                    item
                    for item in plan.items
                    if item.src_abs == source or item.src_abs.startswith(source + os.sep)
                ]
                if (related and all(item.src_abs in completed for item in related)) or (
                    not related and not result.cancelled and not result.failed and source in plan.source_dirs
                ):
                    src_panel.marked.discard(source)
            src_panel.refresh_listing()
            dst_panel.refresh_listing()
            summary = f"{result.copied} copied, {len(result.skipped)} skipped, {len(result.failed)} failed"
            if kind == OpKind.MOVE and result.identical_kept:
                summary += f"\n{result.identical_kept} identical files stayed in the source"
            if result.cancelled:
                owner.app.push_screen(MessageScreen(CANCELLED_TEXT + "\n\n" + summary))
            elif result.failed:
                errors = "\n".join(f"{path}: {error}" for path, error in result.failed[:10])
                owner.app.push_screen(MessageScreen(summary + "\n" + errors))
            else:
                owner.notify(summary)

        owner.call_after_refresh(lambda: owner.run_worker(work, thread=True))

    owner.call_after_refresh(lambda: owner.run_worker(scan, thread=True))
