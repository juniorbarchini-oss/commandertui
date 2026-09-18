from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ProgressBar, Static

from .models import OperationQueue


class ProgressScreen(ModalScreen[None]):
    """Shown while an OperationQueue actually runs. Has no bindings of its
    own on purpose -- a copy/move/delete in flight should not be interrupted
    by a stray keypress; cancellation isn't wired up yet.
    """

    DEFAULT_CSS = """
    ProgressScreen {
        align: center middle;
    }
    #progress-box {
        width: 60;
        height: auto;
        border: solid $foreground;
        padding: 1 2;
    }
    #progress-label {
        margin-bottom: 1;
    }
    """

    def __init__(self, title: str, total_ops: int) -> None:
        super().__init__()
        self.title_text = title
        self.total_ops = total_ops

    def compose(self) -> ComposeResult:
        with Vertical(id="progress-box"):
            yield Static(self.title_text, id="progress-label")
            yield ProgressBar(total=self.total_ops, id="progress-bar", show_eta=False)

    def report(self, done: int, total: int, current_name: str) -> None:
        """Called from the worker thread via app.call_from_thread -- safe to
        touch widgets here, Textual serializes it onto the UI thread."""
        self.query_one(ProgressBar).update(progress=done)
        label = self.query_one("#progress-label", Static)
        label.update(f"{self.title_text}\n{done}/{total}  {current_name}")
