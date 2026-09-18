from __future__ import annotations

from typing import Callable

from .executor import ExecutionReport, execute
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
            def on_progress(done: int, total: int, name: str) -> None:
                owner.app.call_from_thread(progress.report, done, total, name)

            report = execute(queue, left_root, right_root, on_progress=on_progress)

            def finish() -> None:
                progress.dismiss()
                if after:
                    after(report)
                if report.failed:
                    lines = "\n".join(f"  {p}: {e}" for p, e in report.failed[:10])
                    owner.app.push_screen(
                        MessageScreen(f"{report.succeeded} ok, {len(report.failed)} failed:\n{lines}")
                    )

            owner.app.call_from_thread(finish)

        owner.run_worker(work, thread=True)

    owner.app.push_screen(ConfirmScreen(title, queue), on_confirmed)
