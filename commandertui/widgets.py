from __future__ import annotations

import fnmatch
import os
import shutil
from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Button, DataTable, Static

from .scanner import path_size

PARENT_MARK = ".."
SPINNER_FRAMES = "|/-\\"


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def _format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _free_space_label(path: str) -> str:
    """"48.2 GB free" for the filesystem under `path`, or "" if it can't say
    (permission, vanished mount, FUSE that reports nothing)."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return ""
    if usage.total <= 0:
        return ""
    return f"{_format_bytes(usage.free)} free"


class FilePanel(Vertical):
    """One side of the dual-pane view: a single-directory listing you can
    navigate, mark entries in, and read the current path from.
    """

    path: reactive[str] = reactive(os.path.expanduser("~"))
    active: reactive[bool] = reactive(False)
    show_hidden: reactive[bool] = reactive(False)

    def __init__(self, start_path: str, side: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.side = side  # "left" | "right"
        self.marked: set[str] = set()
        self.path = os.path.abspath(start_path)
        self._rows: list[tuple[str, bool]] = []  # (name, is_dir), in display order
        self._sel_generation = 0  # lets a stale background size count be ignored
        self.filter_text = ""
        self._entries: list[tuple[str, str, bool, str, str]] = []
        self._spinner = None  # Timer animating the label while folders are counted

    def compose(self) -> ComposeResult:
        with Horizontal(classes="pathrow"):
            yield Static(self.path, id=f"{self.side}-pathbar", classes="pathbar")
            yield Static("", id=f"{self.side}-sel", classes="pathbar pathsel")
            yield Static("", id=f"{self.side}-free", classes="pathbar pathfree")
        table = DataTable(id=f"{self.side}-table", cursor_type="row")
        table.add_columns(" ", "Name", "Size", "Modified")
        yield table

    def on_mount(self) -> None:
        self.refresh_listing()

    def watch_active(self, active: bool) -> None:
        for suffix in ("pathbar", "sel", "free"):
            self.query_one(f"#{self.side}-{suffix}", Static).set_class(active, "active")
        self.set_class(active, "active-panel")

    def refresh_listing(self) -> None:
        """Re-read the directory from disk, then draw it."""
        self._scan()
        self._draw_rows()
        self.query_one(f"#{self.side}-free", Static).update(_free_space_label(self.path))
        self.update_selection()

    def _scan(self) -> None:
        entries: list[os.DirEntry] = []
        try:
            with os.scandir(self.path) as it:
                entries = sorted(
                    (e for e in it if self.show_hidden or not e.name.startswith(".")),
                    key=lambda e: (not e.is_dir(), e.name.lower()),
                )
        except (PermissionError, FileNotFoundError):
            pass

        self._entries = []  # (name, full path, is_dir, size text, mtime text)
        for entry in entries:
            try:
                stat = entry.stat(follow_symlinks=False)
            except (PermissionError, FileNotFoundError):
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            size = "<DIR>" if is_dir else _human_size(stat.st_size)
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            self._entries.append((entry.name, entry.path, is_dir, size, mtime))

    def _draw_rows(self) -> None:
        """Draw the scanned entries through the current filter. No disk access,
        so typing in the filter never re-reads a slow network share."""
        table = self.query_one(f"#{self.side}-table", DataTable)
        table.clear()
        self._rows = []

        if os.path.dirname(self.path) != self.path:
            table.add_row("", PARENT_MARK, "", "", key=PARENT_MARK)
            self._rows.append((PARENT_MARK, True))

        needle = self.filter_text.lower()
        for name, full, is_dir, size, mtime in self._entries:
            if needle and needle not in name.lower():
                continue
            mark = "*" if full in self.marked else ""
            table.add_row(mark, f"{name}/" if is_dir else name, size, mtime, key=name)
            self._rows.append((name, is_dir))

        shown = self.path + (f"   [filter: {self.filter_text}]" if self.filter_text else "")
        self.query_one(f"#{self.side}-pathbar", Static).update(shown)

    # ------------------------------------------------ pattern selection
    def _visible_paths(self) -> list[str]:
        """Full paths of the rows on screen (respects the filter, skips "..")."""
        return [os.path.join(self.path, name) for name, _ in self._rows if name != PARENT_MARK]

    def _redraw_keeping_cursor(self) -> None:
        table = self.query_one(f"#{self.side}-table", DataTable)
        row = table.cursor_row
        self._draw_rows()
        table.move_cursor(row=min(row, max(table.row_count - 1, 0)))
        self.update_selection()

    def _matching(self, pattern: str) -> list[str]:
        pattern = pattern.strip().lower()
        if not any(c in pattern for c in "*?["):
            pattern = f"*{pattern}*"  # plain text means "contains"
        return [p for p in self._visible_paths() if fnmatch.fnmatchcase(os.path.basename(p).lower(), pattern)]

    def mark_matching(self, pattern: str) -> int:
        found = self._matching(pattern) if pattern.strip() else []
        self.marked.update(found)
        self._redraw_keeping_cursor()
        return len(found)

    def unmark_matching(self, pattern: str) -> int:
        found = [p for p in self._matching(pattern) if p in self.marked] if pattern.strip() else []
        self.marked.difference_update(found)
        self._redraw_keeping_cursor()
        return len(found)

    def invert_marks(self) -> None:
        visible = set(self._visible_paths())
        self.marked ^= visible
        self._redraw_keeping_cursor()

    def set_filter(self, text: str) -> None:
        """Show only entries whose name contains `text` (case-insensitive)."""
        self.filter_text = text
        self._draw_rows()

    def watch_path(self, path: str) -> None:
        # A filter belongs to the folder it was typed in.
        self.filter_text = ""

    def _stop_spinner(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    def update_selection(self) -> None:
        """"4 marked  1.2 GB" in the path row, so the volume is known before
        pressing F5/F8. Folder sizes are counted in a worker (a big tree or a
        network share must not freeze the UI); the label shows the files'
        total meanwhile."""
        self._sel_generation += 1
        generation = self._sel_generation
        self._stop_spinner()
        label = self.query_one(f"#{self.side}-sel", Static)
        if not self.marked:
            label.update("")
            return
        dirs = [p for p in self.marked if os.path.isdir(p) and not os.path.islink(p)]
        files_total = 0
        for path in self.marked:
            if path not in dirs:
                try:
                    files_total += os.path.getsize(path)
                except OSError:
                    pass
        text = f"{len(self.marked)} marked  "
        if not dirs:
            label.update(text + _format_bytes(files_total))
            return
        frame = 0

        def spin() -> None:
            nonlocal frame
            label.update(text + f"{_format_bytes(files_total)} + folders {SPINNER_FRAMES[frame % 4]}")
            frame += 1

        spin()
        self._spinner = self.set_interval(0.12, spin)

        def work() -> None:
            total = files_total + sum(path_size(d) for d in dirs)
            self.app.call_from_thread(done, total)

        def done(total: int) -> None:
            if generation == self._sel_generation and self.is_mounted:
                self._stop_spinner()
                label.update(text + _format_bytes(total))

        self.run_worker(work, thread=True)

    def move_cursor_to(self, name: str) -> None:
        """Put the cursor on `name` (e.g. after a rename); no-op if absent."""
        for index, (row_name, _) in enumerate(self._rows):
            if row_name == name:
                self.query_one(f"#{self.side}-table", DataTable).move_cursor(row=index)
                return

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
        self.update_selection()

    def marked_or_selected(self) -> list[str]:
        """What an action should operate on: the marked set if non-empty,
        otherwise just whatever is under the cursor."""
        if self.marked:
            return sorted(self.marked)
        name = self.selected_name()
        if name and name != PARENT_MARK:
            return [os.path.join(self.path, name)]
        return []


class FnButton(Button, can_focus=False):
    """A clickable key hint. Never takes focus, so the file list keeps it."""


class ActionBar(Horizontal):
    """A row of boxed key hints (replaces the one-line Footer): each box shows
    the key and runs the same action on the current screen when clicked."""

    KEYS: list[tuple[str, str, str]] = []  # (key, label, action)
    PREFIX = "fn"  # button ids are PREFIX-<n>; unique per bar on a screen

    @classmethod
    def needed_width(cls) -> int:
        """Columns the boxes take side by side: label plus 2 border and 2 padding cells."""
        return sum(len(f"{key} {label}") + 4 for key, label, _ in cls.KEYS)

    def compose(self) -> ComposeResult:
        for i, (key, label, action) in enumerate(self.KEYS):
            button = FnButton(f"[b]{key}[/b]\u00a0{label}", id=f"{self.PREFIX}-{i}")
            button.action_name = action
            yield button

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.screen.run_worker(self.screen.run_action(event.button.action_name))


class MainTopBar(ActionBar):
    """Main screen's upper bottom row: panes, views and tools (sits right
    above the file-operation row so all hints live in one place)."""

    PREFIX = "tb"
    KEYS = [
        ("Tab", "Pane", "switch_pane"),
        ("/", "Filter", "filter"),
        ("+", "Select", "select_menu"),
        ("c", "Compare", "show_compare"),
        ("p", "Places", "show_places"),
        ("r", "Refresh", "refresh_both"),
        ("h", "Hidden", "toggle_hidden"),
        ("i", "Size", "show_size"),
        ("q", "Quit", "app.quit"),
    ]


class FunctionBar(ActionBar):
    """Main screen's bottom bar: the classic file-operation keys."""

    PREFIX = "fb"
    KEYS = [
        ("F2", "Rename", "rename"),
        ("F4", "Edit", "edit"),
        ("F5", "Copy", "copy"),
        ("F6", "Move", "move"),
        ("F7", "MkDir", "make_dir"),
        ("F8", "Delete", "delete"),
    ]


class CompareBar(ActionBar):
    """Compare screen's bottom bar."""

    KEYS = [
        ("Esc", "Back", "back"),
        ("m", "Mirror →", "mirror"),
        ("u", "Update →", "update"),
        ("b", "Two-way", "two_way"),
        ("Space", "Mark", "toggle_manual"),
        ("c", "Copy marked →", "copy_manual"),
    ]
