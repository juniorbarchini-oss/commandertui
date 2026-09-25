from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable

from .executor import ExecutionReport, TransferStatus, execute
from .modals import ConfirmScreen, MessageScreen
from .models import OperationQueue
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

            report = execute(
                queue,
                left_root,
                right_root,
                cancel_check=lambda: progress.cancel_requested,
                on_transfer=on_transfer,
            )

            def finish() -> None:
                progress.dismiss()
                if after:
                    after(report)
                if report.cancelled:
                    owner.app.push_screen(
                        MessageScreen(
                            "Cancelled.\n\n"
                            "Files already copied stay at the destination.\n"
                            "The file in progress was discarded - no half-copied files were left.\n"
                            "For a move, the source of anything unfinished is still in place."
                        )
                    )
                elif report.failed:
                    lines = "\n".join(f"  {p}: {e}" for p, e in report.failed[:10])
                    owner.app.push_screen(
                        MessageScreen(f"{report.succeeded} ok, {len(report.failed)} failed:\n{lines}")
                    )

            owner.app.call_from_thread(finish)

        owner.run_worker(work, thread=True)

    owner.app.push_screen(ConfirmScreen(title, queue), on_confirmed)
