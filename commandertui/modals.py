from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from .bookmarks import Place, all_places
from .models import OperationQueue


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class ConfirmScreen(ModalScreen[bool]):
    """A destructive action never runs without this: a plain-language summary
    of exactly what is about to happen, requiring an explicit yes."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 60;
        height: auto;
        border: solid $foreground;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("y", "confirm", "Confirm", priority=True),
        Binding("n", "cancel", "Cancel", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, title: str, queue: OperationQueue) -> None:
        super().__init__()
        self.title_text = title
        self.queue = queue

    def compose(self) -> ComposeResult:
        summary = self.queue.summary()
        by_kind = ", ".join(f"{n} {k}" for k, n in summary["by_kind"].items())
        text = (
            f"{self.title_text}\n\n"
            f"  {summary['count']} operations ({by_kind or 'nothing to do'})\n"
            f"  {_human_bytes(summary['total_bytes'])} total"
        )
        with Vertical(id="confirm-box"):
            yield Static(text)
        # A real Footer, not a hand-written text line: it renders the actual
        # keybindings the same way the rest of the app does, so "how do I
        # confirm this" is never a guess.
        yield Footer()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PlacesScreen(ModalScreen[str | None]):
    """Quick-jump to a mounted drive (USB/SMB/local) or a saved bookmark,
    instead of typing the path."""

    DEFAULT_CSS = """
    PlacesScreen {
        align: center middle;
    }
    #places-box {
        width: 70;
        height: auto;
        max-height: 20;
        border: solid $foreground;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def compose(self) -> ComposeResult:
        table = DataTable(id="places-table", cursor_type="row")
        table.add_columns("Kind", "Label", "Path")
        with Vertical(id="places-box"):
            yield table
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#places-table", DataTable)
        self.places: list[Place] = all_places()
        for place in self.places:
            table.add_row(place.kind, place.label, place.path, key=place.path)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(str(event.row_key.value))

    def action_cancel(self) -> None:
        self.dismiss(None)


class MessageScreen(ModalScreen[None]):
    """A short result notice (e.g. failures after an operation ran)."""

    DEFAULT_CSS = """
    MessageScreen {
        align: center middle;
    }
    #msg-box {
        width: 60;
        height: auto;
        border: solid $danger;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("enter", "close", "Close", priority=True),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="msg-box"):
            yield Static(self.text)
        yield Footer()

    def action_close(self) -> None:
        self.dismiss(None)
