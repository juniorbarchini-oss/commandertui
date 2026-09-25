from __future__ import annotations

import os

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from .compare_screen import CompareScreen
from .modals import InputScreen, MessageScreen, PlacesScreen, SizeScreen
from .models import OpKind, OpSide, OperationQueue, QueuedOp
from .run_queue import run_queue_with_progress
from .scanner import path_size
from .widgets import FilePanel


class MainScreen(Screen):
    """The dual-pane view and its keybindings.

    Kept as its own Screen (rather than living directly on the App) so its
    priority bindings only take effect while it is the active screen --
    Textual checks an App's own priority bindings even when a different
    screen (BootScreen, CompareScreen, a modal) is on top of the stack, so
    anything pane-specific has to live here, not on CommanderApp.
    """

    # priority=True: the focused DataTable has built-in bindings for several
    # of these same keys (notably Enter, which it uses internally to fire
    # RowSelected) that would otherwise intercept the key before our action
    # ever runs.
    BINDINGS = [
        Binding("tab", "switch_pane", "Switch pane", priority=True),
        Binding("enter", "enter_dir", "Open", priority=True),
        Binding("backspace", "up_dir", "Up", priority=True),
        Binding("space", "toggle_mark", "Mark", priority=True),
        Binding("f5", "copy", "Copy", priority=True),
        Binding("f6", "move", "Move", priority=True),
        Binding("f7", "make_dir", "MkDir", priority=True),
        Binding("f8", "delete", "Delete", priority=True),
        Binding("delete", "delete", "Delete", priority=True),
        Binding("c", "show_compare", "Compare", priority=True),
        Binding("p", "show_places", "Places", priority=True),
        Binding("r", "refresh_both", "Refresh", priority=True),
        Binding("h", "toggle_hidden", "Hidden", priority=True),
        Binding("i", "show_size", "Size", priority=True),
        # "app.quit", not "quit": this binding lives on the Screen, and a bare
        # "quit" resolves against the Screen's own (nonexistent) action_quit
        # rather than bubbling to the App, so it silently did nothing.
        Binding("q", "app.quit", "Quit", priority=True),
    ]

    def __init__(self, left_path: str, right_path: str) -> None:
        super().__init__()
        self.left_path = left_path
        self.right_path = right_path
        self.active_side = "left"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            yield FilePanel(self.left_path, side="left", id="panel-left")
            yield FilePanel(self.right_path, side="right", id="panel-right")
        yield Footer()

    def on_mount(self) -> None:
        self._set_active("left")

    # ------------------------------------------------------------ helpers
    def _panel(self, side: str) -> FilePanel:
        return self.query_one(f"#panel-{side}", FilePanel)

    def _active_panel(self) -> FilePanel:
        return self._panel(self.active_side)

    def _other_panel(self) -> FilePanel:
        return self._panel("right" if self.active_side == "left" else "left")

    def _set_active(self, side: str) -> None:
        self.active_side = side
        self._panel("left").active = side == "left"
        self._panel("right").active = side == "right"
        # Marking a panel "active" only recolors its path bar; arrow-key
        # navigation is driven by Textual's real focus, on the DataTable
        # itself, so it has to move too or the cursor keeps responding to
        # whichever pane was focused first regardless of which one looks active.
        self._panel(side).query_one(DataTable).focus()

    # -------------------------------------------------------------- nav
    def action_switch_pane(self) -> None:
        self._set_active("right" if self.active_side == "left" else "left")

    def action_enter_dir(self) -> None:
        self._active_panel().enter_selected()

    def action_up_dir(self) -> None:
        panel = self._active_panel()
        panel.path = os.path.dirname(panel.path)
        panel.marked.clear()
        panel.refresh_listing()

    def action_toggle_mark(self) -> None:
        self._active_panel().toggle_mark_selected()

    def action_refresh_both(self) -> None:
        self._panel("left").refresh_listing()
        self._panel("right").refresh_listing()

    def action_toggle_hidden(self) -> None:
        show = not self._panel("left").show_hidden
        for side in ("left", "right"):
            panel = self._panel(side)
            panel.show_hidden = show
            panel.refresh_listing()

    def action_show_size(self) -> None:
        items = self._active_panel().marked_or_selected()
        if items:
            self.app.push_screen(SizeScreen(items))

    # ------------------------------------------------------------- mkdir
    def action_make_dir(self) -> None:
        panel = self._active_panel()

        def on_named(name: str | None) -> None:
            if not name:
                return
            target = os.path.join(panel.path, name)
            try:
                os.makedirs(target)
            except FileExistsError:
                self.app.push_screen(MessageScreen(f"'{name}' already exists."))
                return
            except OSError as exc:
                self.app.push_screen(MessageScreen(f"Could not create '{name}': {exc}"))
                return
            panel.refresh_listing()

        self.app.push_screen(InputScreen("New folder name:"), on_named)

    # --------------------------------------------------------- op queues
    def action_copy(self) -> None:
        self._run_marked_op(OpKind.COPY, "Copy")

    def action_move(self) -> None:
        self._run_marked_op(OpKind.MOVE, "Move")

    def action_delete(self) -> None:
        src_panel = self._active_panel()
        items = src_panel.marked_or_selected()
        if not items:
            return

        side = OpSide.LEFT if src_panel.side == "left" else OpSide.RIGHT
        queue = OperationQueue()
        for item in items:
            rel_path = os.path.relpath(item, src_panel.path)
            queue.add(QueuedOp(OpKind.DELETE, side, rel_path, os.path.isdir(item), size=path_size(item)))

        def after(report) -> None:
            src_panel.marked.clear()
            src_panel.refresh_listing()

        run_queue_with_progress(
            self, "Delete (to trash)", queue, self._panel("left").path, self._panel("right").path, after
        )

    def _run_marked_op(self, kind: OpKind, label: str) -> None:
        src_panel = self._active_panel()
        dst_panel = self._other_panel()
        items = src_panel.marked_or_selected()
        if not items:
            return

        side = OpSide.LEFT_TO_RIGHT if src_panel.side == "left" else OpSide.RIGHT_TO_LEFT
        queue = OperationQueue()
        for item in items:
            rel_path = os.path.relpath(item, src_panel.path)
            queue.add(QueuedOp(kind, side, rel_path, os.path.isdir(item), size=path_size(item)))

        def after(report) -> None:
            src_panel.marked.clear()
            src_panel.refresh_listing()
            dst_panel.refresh_listing()

        run_queue_with_progress(
            self,
            f"{label} to {dst_panel.path}",
            queue,
            self._panel("left").path,
            self._panel("right").path,
            after,
        )

    # ------------------------------------------------------------- misc
    def action_show_compare(self) -> None:
        left = self._panel("left").path
        right = self._panel("right").path
        self.app.push_screen(CompareScreen(left, right))

    def action_show_places(self) -> None:
        def on_picked(path: str | None) -> None:
            if not path:
                return
            panel = self._active_panel()
            panel.path = path
            panel.marked.clear()
            panel.refresh_listing()

        self.app.push_screen(PlacesScreen(), on_picked)
