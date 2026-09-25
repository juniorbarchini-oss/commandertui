from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, ProgressBar, Static

from .executor import TransferStatus


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _human_rate(bytes_per_sec: float) -> str:
    # Bits, decimal units: comparable with network link speeds.
    mbit = bytes_per_sec * 8 / 1_000_000
    return f"{mbit / 1000:.2f} Gb/s" if mbit >= 1000 else f"{mbit:.1f} Mb/s"


def _human_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


class ProgressScreen(ModalScreen[None]):
    """Shown while an OperationQueue runs: one bar for the current file, one
    for the whole job (by bytes). Escape asks before cancelling, so a stray
    keypress can't stop a transfer by itself."""

    DEFAULT_CSS = """
    ProgressScreen {
        align: center middle;
    }
    #progress-box {
        width: 72;
        height: auto;
        border: solid $foreground;
        padding: 1 2;
    }
    #progress-box ProgressBar {
        margin-bottom: 1;
    }
    #progress-title, #progress-rate {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "ask_cancel", "Cancel", priority=True),
        Binding("y", "confirm_cancel", "Yes, cancel", priority=True),
        Binding("n", "resume", "No, continue", priority=True),
    ]

    def __init__(self, title: str, total_ops: int) -> None:
        super().__init__()
        self.title_text = title
        self.total_ops = total_ops
        self.cancel_requested = False
        self.asking = False
        self.started = time.monotonic()

    def compose(self) -> ComposeResult:
        with Vertical(id="progress-box"):
            yield Static(f"{self.title_text}\n\n  Preparing...", id="progress-title")
            yield Static("", id="file-label")
            yield ProgressBar(total=100, id="file-bar", show_eta=False)
            yield Static("", id="total-label")
            yield ProgressBar(total=100, id="total-bar", show_eta=False)
            yield Static("", id="progress-rate")
        yield Footer()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("confirm_cancel", "resume"):
            return self.asking
        if action == "ask_cancel":
            return not self.asking and not self.cancel_requested
        return True

    def action_ask_cancel(self) -> None:
        self.asking = True
        self.refresh_bindings()
        self.query_one("#progress-rate", Static).update(
            "Cancel? The file being copied is discarded; finished files stay."
        )

    def action_confirm_cancel(self) -> None:
        self.asking = False
        self.cancel_requested = True
        self.refresh_bindings()
        self.query_one("#progress-rate", Static).update("Cancelling...")

    def action_resume(self) -> None:
        self.asking = False
        self.refresh_bindings()

    def report(self, s: TransferStatus) -> None:
        """Called via app.call_from_thread from the worker -- safe to touch widgets."""
        self.query_one("#progress-title", Static).update(self.title_text)

        if s.bytes_total:
            self.query_one("#file-label", Static).update(
                f"File: {s.file_name}   {_human_bytes(s.file_done)} of {_human_bytes(s.file_total)}"
            )
            file_pct = 100 * s.file_done / s.file_total if s.file_total else 100
            self.query_one("#file-bar", ProgressBar).update(progress=file_pct)
            self.query_one("#total-label", Static).update(
                f"Total: file {s.files_done:,} of {s.files_total:,}   "
                f"{_human_bytes(s.bytes_done)} of {_human_bytes(s.bytes_total)}"
            )
            total_pct = 100 * s.bytes_done / s.bytes_total
        else:  # nothing byte-sized (deletes, empty files): count items instead
            self.query_one("#total-label", Static).update(f"Total: {s.ops_done} of {s.ops_total} items")
            total_pct = 100 * s.ops_done / s.ops_total if s.ops_total else 100
        self.query_one("#total-bar", ProgressBar).update(progress=total_pct)

        if self.asking or self.cancel_requested:
            return
        elapsed = time.monotonic() - self.started
        if s.bytes_total and s.bytes_done and elapsed > 1:
            rate = s.bytes_done / elapsed
            left = (s.bytes_total - s.bytes_done) / rate
            self.query_one("#progress-rate", Static).update(
                f"{_human_rate(rate)}  -  about {_human_duration(left)} left"
            )
