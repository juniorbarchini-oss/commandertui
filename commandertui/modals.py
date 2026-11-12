from __future__ import annotations

from collections.abc import Callable

import os
import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, OptionList, Static

from .bookmarks import Place, all_places, remove_bookmark, save_bookmark
from .models import OpKind, OperationQueue
from .netlocations import is_smb_mount_point, local_path, remove_location, unmount
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

    def __init__(self, title: str, queue: OperationQueue, warning: bool = False) -> None:
        super().__init__()
        self.title_text = title
        self.queue = queue
        self.destructive = warning or any(op.kind == OpKind.DELETE for op in queue.ops)
        self.permanent = any(op.permanent for op in queue.ops)

    def compose(self) -> ComposeResult:
        summary = self.queue.summary()
        by_kind = ", ".join(f"{n} {k}" for k, n in summary["by_kind"].items())
        listed = [op.rel_path + ("/" if op.is_dir else "") for op in self.queue.ops[: self.MAX_LISTED]]
        extra = len(self.queue.ops) - len(listed)
        if extra > 0:
            listed.append(f"... and {extra} more")
        with Vertical(id="confirm-box", classes="danger" if self.destructive else ""):
            yield Static(self.title_text, id="confirm-title", markup=False)
            yield Static(
                id="confirm-summary",
                content=f"{summary['count']} operations ({by_kind or 'nothing to do'})"
                + (
                    f", {_human_bytes(summary['total_bytes'])} total"
                    if summary["total_bytes"] is not None
                    else ""
                ),
            )
            yield Static("\n".join(f"  {name}" for name in listed), id="confirm-items", markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button(
                    "Delete permanently" if self.permanent else "Yes  (y)",
                    id="yes",
                    classes="danger" if self.destructive else "",
                )
                yield Button("Cancel" if self.permanent else "No  (n)", id="no")

    def on_mount(self) -> None:
        self.query_one("#no" if self.destructive else "#yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


CHANGE_LOGIN = "\0change-login:"  # prefix PlacesScreen dismisses with, followed by the saved location's URI
ADD_NETWORK = "\0add-network"  # sentinel PlacesScreen dismisses with to start the add-network flow


class PlacesScreen(ModalScreen[str | None]):
    """Quick-jump to a mounted drive (USB/SMB/local) or a saved bookmark,
    instead of typing the path."""

    DEFAULT_CSS = """
    PlacesScreen {
        align: center middle;
    }
    #places-box {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 90%;
        border: heavy $foreground;
        padding: 1 2;
    }
    #places-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #places-table {
        height: auto;
        max-height: 16;
    }
    #places-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #places-buttons Button {
        min-width: 16;
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("a", "add_network", "Add network", priority=True),
        Binding("s", "save_here", "Save here", priority=True),
        Binding("d", "delete_saved", "Delete saved", priority=True),
        Binding("e", "unmount_share", "Unmount share", priority=True),
        Binding("l", "change_login", "Change login", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, current_path: str | None = None) -> None:
        super().__init__()
        self.current_path = current_path

    def compose(self) -> ComposeResult:
        table = DataTable(id="places-table", cursor_type="row")
        table.add_columns("Kind", "Label", "Path")
        with Vertical(id="places-box"):
            yield Static("Places", id="places-title")
            yield table
            with Horizontal(id="places-buttons"):
                yield Button("Add network  (a)", id="add")
                yield Button("Save here  (s)", id="save")
                yield Button("Delete saved  (d)", id="delete")
                yield Button("Unmount share  (e)", id="unmount")
                yield Button("Change login  (l)", id="login")
                yield Button("Close  (Esc)", id="close")

    def on_mount(self) -> None:
        self._fill()

    def _fill(self) -> None:
        table = self.query_one("#places-table", DataTable)
        current_row = table.cursor_row
        table.clear()
        self.places: list[Place] = all_places()
        for place in self.places:
            table.add_row(place.kind, place.label, place.path, key=place.path)
        if self.places:
            table.move_cursor(row=min(current_row, len(self.places) - 1))

    def action_add_network(self) -> None:
        self.dismiss(ADD_NETWORK)

    def action_save_here(self) -> None:
        """Bookmark the folder the active pane is in, under a name you pick."""
        if not self.current_path:
            return
        path = self.current_path

        def on_named(label: str | None) -> None:
            if label:
                save_bookmark(label, path)
                self._fill()

        self.app.push_screen(
            InputScreen(f"Save {path} as:", initial=os.path.basename(path.rstrip("/")) or path), on_named
        )

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
            self.notify("Only saved places can be deleted.", severity="warning")
            return
        self._fill()

    def _evict_panels_from(self, mounted_path: str) -> None:
        home = os.path.expanduser("~")
        prefix = mounted_path.rstrip(os.sep) + os.sep
        for side in ("left", "right"):
            try:
                panel = self.app._panel(side)
            except Exception:
                continue
            if panel.path == mounted_path or panel.path.startswith(prefix):
                panel.path = home
                panel.marked.clear()
                panel.refresh_listing()

    def action_change_login(self) -> None:
        """Forget the session of a saved network location to log in again (anonymous or another user)."""
        table = self.query_one("#places-table", DataTable)
        if not self.places or table.cursor_row >= len(self.places):
            return
        place = self.places[table.cursor_row]
        if place.kind != "net-saved":
            self.notify("Pick a saved network location to change its login.", severity="warning")
            return
        self.dismiss(CHANGE_LOGIN + place.path)

    def action_unmount_share(self) -> None:
        table = self.query_one("#places-table", DataTable)
        if not self.places or table.cursor_row >= len(self.places):
            return
        place = self.places[table.cursor_row]
        mounted: str | None = None
        if place.kind == "mounted" and is_smb_mount_point(place.path):
            mounted = place.path
        elif (place.kind == "net-saved" and place.path.lower().startswith("smb://")) or (
            place.kind == "network" and "smb" in os.path.basename(place.path).lower()
        ):
            mounted = local_path(place.path)

        if not mounted:
            self.app.push_screen(MessageScreen("Not a mounted network location"))
            return
        self._evict_panels_from(mounted)
        ok, err = unmount(place.path)
        if not ok:
            self.app.push_screen(MessageScreen(f"Could not unmount {place.label}.\n{err}"))
            return
        self.notify(f"Unmounted {place.label}.")
        self._fill()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {
            "add": self.action_add_network,
            "save": self.action_save_here,
            "delete": self.action_delete_saved,
            "unmount": self.action_unmount_share,
            "login": self.action_change_login,
        }.get(event.button.id, self.action_cancel)()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(str(event.row_key.value))

    def action_cancel(self) -> None:
        self.dismiss(None)


class InputScreen(ModalScreen[str | None]):
    """A single text prompt (e.g. new folder name). Enter or the OK button
    confirms with the current text, Escape or Cancel dismisses with None --
    same shape as ConfirmScreen so callers can use the same
    push_screen(..., callback) pattern."""

    DEFAULT_CSS = """
    InputScreen {
        align: center middle;
    }
    #input-box {
        width: 80%;
        min-width: 50;
        max-width: 90;
        height: auto;
        border: heavy $foreground;
        padding: 1 3;
    }
    #input-prompt {
        text-style: bold;
        margin-bottom: 1;
    }
    #input-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #input-buttons Button {
        min-width: 18;
        margin: 0 2;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, prompt: str, initial: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="input-box"):
            yield Static(self.prompt, id="input-prompt")
            yield Input(value=self.initial, id="input-field")
            with Horizontal(id="input-buttons"):
                yield Button("OK  (Enter)", id="ok")
                yield Button("Cancel  (Esc)", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _submit(self) -> None:
        self.dismiss(self.query_one(Input).value.strip() or None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

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
        width: 80%;
        min-width: 50;
        max-width: 90;
        height: auto;
        border: heavy $foreground;
        padding: 1 3;
    }
    #size-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #size-buttons Button {
        min-width: 18;
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
            with Horizontal(id="size-buttons"):
                yield Button("Close  (Esc)", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_close()

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
        width: 80%;
        min-width: 50;
        max-width: 90;
        height: auto;
        border: heavy $foreground;
        padding: 1 3;
    }
    #msg-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #msg-buttons Button {
        min-width: 18;
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
            with Horizontal(id="msg-buttons"):
                yield Button("OK  (Enter)", id="ok")

    def on_mount(self) -> None:
        self.query_one("#ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_close()

    def action_close(self) -> None:
        self.dismiss(None)


class ChoiceScreen(ModalScreen[str | None]):
    """Pick one item from a short list; Escape cancels with None."""

    DEFAULT_CSS = """
    ChoiceScreen {
        align: center middle;
    }
    #choice-box {
        width: 80%;
        min-width: 50;
        max-width: 90;
        height: auto;
        max-height: 90%;
        border: heavy $foreground;
        padding: 1 3;
    }
    #choice-prompt {
        text-style: bold;
        margin-bottom: 1;
    }
    #choice-list {
        height: auto;
        max-height: 14;
    }
    #choice-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #choice-buttons Button {
        min-width: 18;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(
        self,
        prompt: str,
        options: list[str],
        initial: int | None = None,
        on_highlight: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__()
        self.prompt = prompt
        self.options = options
        self.initial = initial
        self.on_highlight = on_highlight  # lets a picker preview each item as you move

    def on_mount(self) -> None:
        if self.initial is not None:
            self.query_one("#choice-list", OptionList).highlighted = self.initial

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.on_highlight is not None:
            self.on_highlight(event.option_index)

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-box"):
            yield Static(self.prompt, id="choice-prompt")
            yield OptionList(*self.options, id="choice-list")
            with Horizontal(id="choice-buttons"):
                yield Button("Cancel  (Esc)", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.options[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class FilterScreen(ModalScreen[str | None]):
    """Live name filter for a pane: the list behind shrinks as you type.
    Enter (Apply) keeps the filter, Esc (Clear) removes it."""

    DEFAULT_CSS = """
    FilterScreen {
        align: center bottom;
    }
    #filter-box {
        width: 80%;
        min-width: 50;
        max-width: 90;
        height: auto;
        border: heavy $foreground;
        padding: 0 3;
        margin-bottom: 8;
    }
    #filter-buttons {
        height: 3;
        align: center middle;
    }
    #filter-buttons Button {
        min-width: 18;
        margin: 0 2;
    }
    """

    BINDINGS = [Binding("escape", "clear", "Clear", priority=True)]

    def __init__(self, initial: str, on_change) -> None:
        super().__init__()
        self.initial = initial
        self.on_change = on_change

    def compose(self) -> ComposeResult:
        with Vertical(id="filter-box"):
            yield Static("Filter by name (contains, ignores case):", id="filter-prompt")
            yield Input(value=self.initial, id="filter-field")
            with Horizontal(id="filter-buttons"):
                yield Button("Apply  (Enter)", id="apply")
                yield Button("Clear  (Esc)", id="clear")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.on_change(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply":
            self.dismiss(self.query_one(Input).value)
        else:
            self.action_clear()

    def action_clear(self) -> None:
        self.dismiss(None)


class UpdateScreen(ModalScreen[str]):
    """A newer version exists. Dismisses with "now", "later" or "skip"; the
    app only upgrades after "now"."""

    DEFAULT_CSS = """
    UpdateScreen {
        align: center middle;
    }
    #update-box {
        width: 80%;
        min-width: 56;
        max-width: 90;
        height: auto;
        border: heavy $foreground;
        padding: 1 3;
    }
    #update-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #update-how {
        margin-bottom: 1;
    }
    #update-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #update-buttons Button {
        min-width: 16;
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "later", "Later", priority=True),
        Binding("s", "skip", "Skip this version", priority=True),
    ]

    HOW = {
        "pacman": "Installed as an Arch package: this downloads the release and rebuilds it "
        "with makepkg (a few seconds). The terminal asks for your sudo password to "
        "install it; it is typed there, not here.",
        "pipx": "Installed with pipx: this runs  pipx upgrade commandertui.",
        "pip": "Installed with pip: this runs  pip install --upgrade commandertui.",
        "source": "You are running from a git checkout.\nUpdate it yourself with  git pull.",
    }

    def __init__(self, info) -> None:
        super().__init__()
        self.info = info

    def compose(self) -> ComposeResult:
        with Vertical(id="update-box"):
            yield Static(
                f"CommanderTUI {self.info.latest} is available (you have {self.info.current})",
                id="update-title",
            )
            yield Static(self.HOW.get(self.info.install, ""), id="update-how", markup=False)
            with Horizontal(id="update-buttons"):
                if self.info.install != "source":
                    yield Button("Update now", id="now")
                yield Button("Later  (Esc)", id="later")
                yield Button("Skip  (s)", id="skip")

    def on_mount(self) -> None:
        self.query_one("#now" if self.info.install != "source" else "#later", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_later(self) -> None:
        self.dismiss("later")

    def action_skip(self) -> None:
        self.dismiss("skip")


class ConflictScreen(ModalScreen):
    """One safe decision for a batch or one collision; no filesystem changes."""

    DEFAULT_CSS = """
    ConflictScreen { align: center middle; }
    #conflict-box { width: 80%; min-width: 60; max-width: 100; height: auto; max-height: 95%;
        border: heavy $foreground; padding: 1 3; overflow: hidden hidden; }
    ConflictScreen.compact #conflict-box { padding: 0 2; }
    #conflict-title { text-style: bold; margin-bottom: 1; max-height: 2; }
    #conflict-summary { height: auto; margin-bottom: 1; overflow: hidden auto; }
    #conflict-buttons { height: auto; overflow: hidden hidden; }
    #conflict-buttons Button { position: absolute; min-width: 0; height: 3; margin: 0; }
    #apply-remaining { margin-top: 1; }
    ConflictScreen.compact #apply-remaining { margin-top: 0; }
    """

    BINDINGS = [
        Binding(key, f"choose('{value}')", label, priority=True)
        for key, value, label in [
            ("o", "overwrite", "Overwrite"),
            ("s", "skip", "Skip"),
            ("n", "only_newer", "Only newer"),
            ("r", "rename", "Rename"),
            ("m", "merge", "Merge"),
            ("a", "ask", "Ask each"),
            ("c", "cancel", "Cancel"),
            ("escape", "cancel", "Cancel"),
        ]
    ]

    def __init__(self, report, dst_dir, kind, *, rel_path=None, file_policy=False, summary=None):
        super().__init__()
        from .conflicts import Resolution, allowed_resolutions

        self.report_data = report
        self.dst_dir = dst_dir
        self.kind = kind
        self.rel_path = rel_path
        self.file_policy = file_policy
        self.summary_text = summary
        self.options = [
            r.value
            for r in allowed_resolutions(kind)
            if r != Resolution.MERGE or (report.dir_collisions and not file_policy and rel_path is None)
        ]
        if not file_policy and rel_path is None:
            self.options.insert(-1, "ask")

    def compose(self):
        from textual.widgets import Checkbox

        report = self.report_data
        count = len(
            {c.rel_path for c in report.conflicts}
            | set(report.dir_collisions)
            | set(report.duplicate_sources)
        )
        title = (
            f"Resolve {self.rel_path}"
            if self.rel_path
            else (
                "Choose a policy for conflicting files"
                if self.file_policy
                else f"{count} items already exist"
            )
        )
        entries = report.conflicts
        if self.rel_path:
            entries = [c for c in entries if c.rel_path == self.rel_path]
        from .trash import is_remote_path

        remote_replace = is_remote_path(self.dst_dir) or any(
            is_remote_path(os.path.join(self.dst_dir, conflict.rel_path)) for conflict in report.conflicts
        )
        lines = [f"in {self.dst_dir}"]
        if remote_replace:
            lines.append("Network location: replaced items are deleted permanently; no trash.")
        if report.identical:
            lines.append(f"({report.identical} identical: same size and date)")
        for conflict in entries[:6]:

            def side(entry, label):
                date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.mtime))
                return f"{label}: {_human_bytes(entry.size)}, {date}"

            label = "identical" if conflict.identical else f"newer: {conflict.newer}"
            lines.append(
                f"{conflict.rel_path}: {side(conflict.src, 'Source')} | "
                f"{side(conflict.dst, 'Destination')} — {label}"
            )
        if len(entries) > 6:
            lines.append(f"... and {len(entries) - 6} more")
        if report.dir_collisions:
            lines.append("Existing folders: " + ", ".join(report.dir_collisions[:6]))
        if report.duplicate_sources:
            lines.append("Repeated source names: " + ", ".join(report.duplicate_sources))
        labels = {
            "overwrite": "Overwrite (o)",
            "skip": "Skip (s)",
            "only_newer": "Only newer (n)",
            "rename": "Rename (r)",
            "merge": "Merge (m)",
            "ask": "Ask each (a)",
            "cancel": "Cancel (Esc)",
        }
        if report.dir_collisions and not self.file_policy:
            labels["overwrite"] = "Overwrite (keeps extra files) (o)"
        if remote_replace:
            labels["overwrite"] = "Replace (permanent on network location) (o)"
        with Vertical(id="conflict-box"):
            yield Static(title, id="conflict-title", markup=False)
            with VerticalScroll(id="conflict-summary"):
                yield Static(self.summary_text or "\n".join(lines), id="conflict-list", markup=False)
            with Vertical(id="conflict-buttons"):
                for option in self.options:
                    yield Button(labels[option], id=f"conflict-{option}")
            if self.rel_path:
                yield Checkbox("Apply to all remaining (l)", id="apply-remaining")

    def on_mount(self):
        self._resize_layout()
        self.query_one("#conflict-skip", Button).focus()

    def on_resize(self):
        self._resize_layout()

    def _resize_layout(self):
        self.set_class(self.size.width < 125 or self.size.height < 30, "compact")
        self.call_after_refresh(self._layout_controls)

    def _layout_controls(self):
        """Wrap measured button widths; reserve fixed space outside the list.

        Reposition existing buttons instead of remounting them, preserving
        focus and the Ask each checkbox when the terminal is resized.
        """
        from rich.cells import cell_len

        box = self.query_one("#conflict-box")
        controls = self.query_one("#conflict-buttons")
        buttons = list(controls.query(Button))
        available = controls.content_size.width
        if not available:
            return
        compact = self.has_class("compact")
        margin = 1 if compact else 2
        widths = [
            max(14 if compact else 18, cell_len(str(button.label)) + max(4, button.gutter.width))
            for button in buttons
        ]
        rows = [list(range(min(3, len(buttons)))), list(range(3, len(buttons)))]
        if compact or any(sum(widths[i] + 2 * margin for i in row) > available for row in rows):
            rows = []
            row = []
            used = 0
            for index, width in enumerate(widths):
                if row and used + width + 2 * margin > available:
                    rows.append(row)
                    row, used = [], 0
                row.append(index)
                used += width + 2 * margin
            if row:
                rows.append(row)
        y = 1
        for row in filter(None, rows):
            used = sum(widths[i] + 2 * margin for i in row)
            x = max(0, (available - used) // 2)
            for index in row:
                button = buttons[index]
                button.styles.width = min(widths[index], available - 2 * margin)
                button.styles.offset = (x + margin, y)
                x += widths[index] + 2 * margin
            y += 3 if compact else 4
        controls.styles.height = y if compact else y - 1
        title = self.query_one("#conflict-title")
        summary = self.query_one("#conflict-summary")
        checkbox = self.query("#apply-remaining")
        checkbox_height = (3 + (0 if compact else 1)) if checkbox else 0
        # Auto height keeps the established large-dialog proportions. Only the
        # summary is capped and scrollable; focus can never scroll the box.
        reserved = (
            box.gutter.height
            + min(2, title.get_content_height(box.content_size, self.size, available))
            + title.styles.margin.height
            + int(controls.styles.height.value)
        )
        reserved += checkbox_height + summary.styles.margin.height
        summary.styles.max_height = max(1, int(self.size.height * 0.95) - reserved)

    def action_choose(self, value):
        from textual.widgets import Checkbox

        if value not in self.options:
            return
        apply = self.query_one("#apply-remaining", Checkbox).value if self.rel_path else False
        self.dismiss((value, apply))

    def action_cancel(self):
        self.dismiss(("cancel", False))

    def key_l(self):
        if self.rel_path:
            checkbox = self.query_one("#apply-remaining")
            checkbox.value = not checkbox.value

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        self.action_choose(event.button.id.removeprefix("conflict-"))
