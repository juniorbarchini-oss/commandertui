from __future__ import annotations

import os
from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Static

PARENT_MARK = ".."


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


class FilePanel(Vertical):
    """One side of the dual-pane view: a single-directory listing you can
    navigate, mark entries in, and read the current path from.
    """

    path: reactive[str] = reactive(os.path.expanduser("~"))
    active: reactive[bool] = reactive(False)

    def __init__(self, start_path: str, side: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.side = side  # "left" | "right"
        self.marked: set[str] = set()
        self.path = os.path.abspath(start_path)
        self._rows: list[tuple[str, bool]] = []  # (name, is_dir), in display order

    def compose(self) -> ComposeResult:
        yield Static(self.path, id=f"{self.side}-pathbar", classes="pathbar")
        table = DataTable(id=f"{self.side}-table", cursor_type="row")
        table.add_columns(" ", "Name", "Size", "Modified")
        yield table

    def on_mount(self) -> None:
        self.refresh_listing()

    def watch_active(self, active: bool) -> None:
        bar = self.query_one(f"#{self.side}-pathbar", Static)
        bar.set_class(active, "active")

    def refresh_listing(self) -> None:
        table = self.query_one(f"#{self.side}-table", DataTable)
        table.clear()
        self._rows = []

        entries: list[os.DirEntry] = []
        try:
            with os.scandir(self.path) as it:
                entries = sorted(it, key=lambda e: (not e.is_dir(), e.name.lower()))
        except (PermissionError, FileNotFoundError):
            pass

        if os.path.dirname(self.path) != self.path:
            table.add_row("", PARENT_MARK, "", "", key=PARENT_MARK)
            self._rows.append((PARENT_MARK, True))

        for entry in entries:
            try:
                stat = entry.stat(follow_symlinks=False)
            except (PermissionError, FileNotFoundError):
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            mark = "*" if entry.path in self.marked else ""
            size = "<DIR>" if is_dir else _human_size(stat.st_size)
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            display_name = f"{entry.name}/" if is_dir else entry.name
            table.add_row(mark, display_name, size, mtime, key=entry.name)
            self._rows.append((entry.name, is_dir))

        bar = self.query_one(f"#{self.side}-pathbar", Static)
        bar.update(self.path)

    def selected_name(self) -> str | None:
        table = self.query_one(f"#{self.side}-table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return row_key.value if row_key is not None else None

    def enter_selected(self) -> None:
        name = self.selected_name()
        if name is None:
            return
        if name == PARENT_MARK:
            self.path = os.path.dirname(self.path)
        else:
            candidate = os.path.join(self.path, name)
            if os.path.isdir(candidate):
                self.path = candidate
        self.marked.clear()
        self.refresh_listing()

    def toggle_mark_selected(self) -> None:
        name = self.selected_name()
        if name is None or name == PARENT_MARK:
            return
        full = os.path.join(self.path, name)
        if full in self.marked:
            self.marked.discard(full)
            mark = ""
        else:
            self.marked.add(full)
            mark = "*"

        # Update just the mark cell in place rather than calling
        # refresh_listing(): rebuilding the whole table resets the cursor to
        # row 0, so marking an item used to bounce you straight back to the
        # top -- annoying when marking several items in a row.
        table = self.query_one(f"#{self.side}-table", DataTable)
        table.update_cell_at(table.cursor_coordinate._replace(column=0), mark)

    def marked_or_selected(self) -> list[str]:
        """What an action should operate on: the marked set if non-empty,
        otherwise just whatever is under the cursor."""
        if self.marked:
            return sorted(self.marked)
        name = self.selected_name()
        if name and name != PARENT_MARK:
            return [os.path.join(self.path, name)]
        return []
