from __future__ import annotations

import time
import os
from dataclasses import replace
from typing import Callable

from .executor import ExecutionReport, TransferStatus, execute
from .modals import ConfirmScreen, MessageScreen
from .models import OperationQueue, OpKind, OpSide
from .trash import is_remote_path, remote_location
from .progress_screen import ProgressScreen


def run_queue_with_progress(
    owner,  # Screen: MainScreen or CompareScreen
    title: str,
    queue: OperationQueue,
    left_root: str,
    right_root: str,
    after: Callable[[ExecutionReport], None] | None = None,
) -> None:
    """Confirm, then run an OperationQueue in a background thread with a
    live progress screen.

    Running `execute()` straight on the UI thread (the original approach)
    blocked Textual's event loop for the whole transfer: no repaint, no
    input, the screen just sat frozen until it finished. Everything that
    touches a widget from inside `work()` goes through `call_from_thread`,
    which is the only safe way back onto the UI thread from a worker.
    """
    if len(queue) == 0:
        owner.app.push_screen(MessageScreen("Nothing to do."))
        return

    remote_deletes = []
    remote_replaces = []
    for op in queue.ops:
        if op.kind == OpKind.DELETE:
            root = left_root if op.side == OpSide.LEFT else right_root
            path = os.path.join(root, op.rel_path)
            if is_remote_path(path):
                remote_deletes.append(path)
                op.permanent = True
        elif op.kind in (OpKind.COPY, OpKind.MOVE):
            root = right_root if op.side == OpSide.LEFT_TO_RIGHT else left_root
            path = os.path.join(root, op.rel_path)
            if os.path.lexists(path) and is_remote_path(path):
                remote_replaces.append(path)
    confirm_title = title
    if remote_deletes:
        title = "Delete (remote permanently; local to trash)"
        locations = ", ".join(sorted({remote_location(p) for p in remote_deletes}))
        names = ", ".join(os.path.basename(p) for p in remote_deletes[:3])
        confirm_title = (
            f"{len(remote_deletes)} item(s) on a network location ({locations}): {names}\n"
            "No trash will be used: these items will be deleted permanently.\n"
            "Local items will go to the trash. Delete permanently?"
        )
    if remote_replaces:
        confirm_title += "\nNetwork location: replaced destinations are deleted permanently; no trash."

    def on_confirmed(confirmed: bool | None) -> None:
        if not confirmed:
            return

        progress = ProgressScreen(title, len(queue))
        owner.app.push_screen(progress)

        def work() -> None:
            last = 0.0

            def on_transfer(status: TransferStatus) -> None:
                # Throttled: a per-chunk UI update would flood the event loop.
                nonlocal last
                now = time.monotonic()
                if now - last >= 0.1 or status.file_done == status.file_total:
                    last = now
                    owner.app.call_from_thread(progress.report, replace(status))

            try:
                report = execute(
                    queue,
                    left_root,
                    right_root,
                    cancel_check=lambda: progress.cancel_requested,
                    on_transfer=on_transfer,
                )
            except Exception as error:
                report = ExecutionReport(failed=[("Operation", str(error))])

            def finish() -> None:
                progress.dismiss()
                if after:
                    after(report)
                if report.cancelled:
                    if any(op.kind == OpKind.DELETE for op in queue.ops):
                        message = "Cancelled. Completed deletions were not undone. Remaining items were not started."
                        if remote_deletes:
                            message += "\nPermanent network deletions cannot be restored."
                    else:
                        message = (
                            "Cancelled.\n\nFiles already copied stay at the destination.\n"
                            "The file in progress was discarded - no half-copied files were left.\n"
                            "For a move, the source of anything unfinished is still in place."
                        )
                    owner.app.push_screen(MessageScreen(message))
                elif report.failed:
                    lines = "\n".join(f"  {p}: {e}" for p, e in report.failed[:10])
                    owner.app.push_screen(
                        MessageScreen(f"{report.succeeded} ok, {len(report.failed)} failed:\n{lines}")
                    )

            owner.app.call_from_thread(finish)

        owner.call_after_refresh(lambda: owner.run_worker(work, thread=True))

    owner.app.push_screen(
        ConfirmScreen(confirm_title, queue, warning=bool(remote_deletes or remote_replaces)), on_confirmed
    )
