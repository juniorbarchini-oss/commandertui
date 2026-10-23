from __future__ import annotations

import os
import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, OptionList, Static

from .bookmarks import Place, all_places, remove_bookmark
from .models import OpKind, OperationQueue
from .netlocations import remove_location
from .scanner import tree_stats


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class ConfirmScreen(ModalScreen[bool]):
    """A destructive action never runs without this: a plain-language summary
    of exactly what is about to happen, requiring an explicit yes.

    Yes/No are real buttons inside the box. A delete is drawn in the error
    colour and starts focused on No, so a stray Enter cannot destroy data."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 80%;
        min-width: 60;
        max-width: 100;
        height: auto;
        border: heavy $foreground;
        padding: 1 3;
    }
    #confirm-box.danger {
        border: heavy $error;
    }
    #confirm-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #confirm-items {
        margin-bottom: 0;
    }
    #confirm-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #confirm-buttons Button {
        min-width: 18;
        margin: 0 2;
    }
    #confirm-summary {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("y", "confirm", "Yes", priority=True),
        Binding("n", "cancel", "No", priority=True),
        Binding("escape", "cancel", "No", priority=True),
        Binding("left", "app.focus_previous", "Previous", show=False, priority=True),
        Binding("right", "app.focus_next", "Next", show=False, priority=True),
    ]

    MAX_LISTED = 6

    def __init__(self, title: str, queue: OperationQueue) -> None:
        super().__init__()
        self.title_text = title
        self.queue = queue
        self.destructive = any(op.kind == OpKind.DELETE for op in queue.ops)

    def compose(self) -> ComposeResult:
        summary = self.queue.summary()
        by_kind = ", ".join(f"{n} {k}" for k, n in summary["by_kind"].items())
        listed = [op.rel_path + ("/" if op.is_dir else "") for op in self.queue.ops[: self.MAX_LISTED]]
        extra = len(self.queue.ops) - len(listed)
        if extra > 0:
            listed.append(f"... and {extra} more")
        with Vertical(id="confirm-box", classes="danger" if self.destructive else ""):
            yield Static(self.title_text, id="confirm-title")
            yield Static(
                id="confirm-summary",
                content=f"{summary['count']} operations ({by_kind or 'nothing to do'}), "
                f"{_human_bytes(summary['total_bytes'])} total"
            )
            yield Static("\n".join(f"  {name}" for name in listed), id="confirm-items", markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes  (y)", id="yes", classes="danger" if self.destructive else "")
                yield Button("No  (n)", id="no")

    def on_mount(self) -> None:
        self.query_one("#no" if self.destructive else "#yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


ADD_NETWORK = "\0add-network"  # sentinel PlacesScreen dismisses with to start the add-network flow


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

    BINDINGS = [
        Binding("a", "add_network", "Add network", priority=True),
        Binding("d", "delete_saved", "Delete saved", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def compose(self) -> ComposeResult:
        table = DataTable(id="places-table", cursor_type="row")
        table.add_columns("Kind", "Label", "Path")
        with Vertical(id="places-box"):
            yield table
        yield Footer()

    def on_mount(self) -> None:
        self._fill()

    def _fill(self) -> None:
        table = self.query_one("#places-table", DataTable)
        table.clear()
        self.places: list[Place] = all_places()
        for place in self.places:
            table.add_row(place.kind, place.label, place.path, key=place.path)

    def action_add_network(self) -> None:
        self.dismiss(ADD_NETWORK)

    def action_delete_saved(self) -> None:
        table = self.query_one("#places-table", DataTable)
        if not self.places or table.cursor_row >= len(self.places):
            return
        place = self.places[table.cursor_row]
        if place.kind == "net-saved":
            remove_location(place.path)
        elif place.kind == "saved":
            remove_bookmark(place.path)
        else:
            return
        self._fill()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(str(event.row_key.value))

    def action_cancel(self) -> None:
        self.dismiss(None)


class InputScreen(ModalScreen[str | None]):
    """A single text prompt (e.g. new folder name). Enter confirms with the
    current text, Escape cancels with None -- same shape as ConfirmScreen so
    callers can use the same push_screen(..., callback) pattern."""

    DEFAULT_CSS = """
    InputScreen {
        align: center middle;
    }
    #input-box {
        width: 60;
        height: auto;
        border: solid $foreground;
        padding: 1 2;
    }
    #input-box Static {
        margin-bottom: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="input-box"):
            yield Static(self.prompt)
            yield Input(id="input-field")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SizeScreen(ModalScreen[None]):
    """Counts files/folders/bytes of the given paths in a worker thread, so a
    big tree (or a slow network share) never freezes the UI. Closing it
    early (Escape) stops the count."""

    DEFAULT_CSS = """
    SizeScreen {
        align: center middle;
    }
    #size-box {
        width: 60;
        height: auto;
        border: solid $foreground;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("enter", "close", "Close", priority=True),
    ]

    def __init__(self, paths: list[str]) -> None:
        super().__init__()
        self.paths = paths
        self.cancelled = False
        self.label = os.path.basename(paths[0]) if len(paths) == 1 else f"{len(paths)} items"

    def compose(self) -> ComposeResult:
        with Vertical(id="size-box"):
            yield Static(f"{self.label}\n\n  Calculating...", id="size-text")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._work, thread=True)

    def _work(self) -> None:
        last = 0.0

        def on_progress(files: int, dirs: int, total: int) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last >= 0.2:
                last = now
                self.app.call_from_thread(self._show, files, dirs, total, False)

        files, dirs, total = tree_stats(self.paths, lambda: self.cancelled, on_progress)
        if not self.cancelled:
            self.app.call_from_thread(self._show, files, dirs, total, True)

    def _show(self, files: int, dirs: int, total: int, done: bool) -> None:
        if self.cancelled or not self.is_mounted:
            return
        status = "" if done else "  Calculating...\n"
        self.query_one("#size-text", Static).update(
            f"{self.label}\n\n{status}"
            f"  {files:,} files, {dirs:,} folders\n"
            f"  {_human_bytes(total)}  ({total:,} bytes)"
        )

    def action_close(self) -> None:
        self.cancelled = True
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
        border: solid $error;
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


class ChoiceScreen(ModalScreen[str | None]):
    """Pick one item from a short list; Escape cancels with None."""

    DEFAULT_CSS = """
    ChoiceScreen {
        align: center middle;
    }
    #choice-box {
        width: 60;
        height: auto;
        max-height: 20;
        border: solid $foreground;
        padding: 1 2;
    }
    #choice-box Static {
        margin-bottom: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, prompt: str, options: list[str]) -> None:
        super().__init__()
        self.prompt = prompt
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-box"):
            yield Static(self.prompt)
            yield OptionList(*self.options)
        yield Footer()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.options[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)
