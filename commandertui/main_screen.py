from __future__ import annotations

import os

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Header

from .compare_screen import CompareScreen
from . import netlocations
from .modals import ADD_NETWORK, ChoiceScreen, InputScreen, MessageScreen, PlacesScreen, SizeScreen
from .models import OpKind, OpSide, OperationQueue, QueuedOp
from .run_queue import run_queue_with_progress
from .scanner import path_size
from .widgets import PARENT_MARK, FilePanel, FunctionBar, MainTopBar


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
        Binding("f2", "rename", "Rename", priority=True),
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
        with Horizontal(id="bars"):
            yield MainTopBar()
            yield FunctionBar()

    def on_mount(self) -> None:
        self._set_active("left")
        self._layout_bars()

    def on_resize(self, event) -> None:
        self._layout_bars()

    def _layout_bars(self) -> None:
        """One row of key boxes when the window is wide enough, two stacked
        rows (tools above, F-keys below) when it is not."""
        needed = MainTopBar.needed_width() + FunctionBar.needed_width() + 2
        wide = self.size.width >= needed
        bars = self.query_one("#bars")
        bars.set_class(wide, "wide")
        bars.set_class(not wide, "narrow")

    # ------------------------------------------------------------ helpers
    def _panel(self, side: str) -> FilePanel:
        return self.query_one(f"#panel-{side}", FilePanel)

    def _active_panel(self) -> FilePanel:
        return self._panel(self.active_side)

    def _nothing_selected(self) -> None:
        self.notify("Nothing selected in this pane. Press Tab to switch panes.", severity="warning")

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
    def action_rename(self) -> None:
        panel = self._active_panel()
        name = panel.selected_name()
        if not name or name == PARENT_MARK:
            self._nothing_selected()
            return

        def on_named(new_name: str | None) -> None:
            if not new_name or new_name == name:
                return
            if "/" in new_name or new_name in (".", ".."):
                self.app.push_screen(MessageScreen(f'"{new_name}" is not a valid name.'))
                return
            src = os.path.join(panel.path, name)
            dst = os.path.join(panel.path, new_name)
            # os.rename (not os.replace): never silently overwrite another item.
            if os.path.lexists(dst):
                self.app.push_screen(MessageScreen(f'"{new_name}" already exists here.'))
                return
            try:
                os.rename(src, dst)
            except OSError as e:
                self.app.push_screen(MessageScreen(f"Could not rename:\n{e}"))
                return
            if src in panel.marked:
                panel.marked.discard(src)
                panel.marked.add(dst)
            panel.refresh_listing()
            panel.move_cursor_to(new_name)

        self.app.push_screen(InputScreen("Rename to:", initial=name), on_named)

    def action_copy(self) -> None:
        self._run_marked_op(OpKind.COPY, "Copy")

    def action_move(self) -> None:
        self._run_marked_op(OpKind.MOVE, "Move")

    def action_delete(self) -> None:
        src_panel = self._active_panel()
        items = src_panel.marked_or_selected()
        if not items:
            self._nothing_selected()
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
            self._nothing_selected()
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
            if path == ADD_NETWORK:
                self._add_network()
            elif netlocations.is_uri(path):
                self._connect(path)
            else:
                self._go_to(path)

        self.app.push_screen(PlacesScreen(self._active_panel().path), on_picked)

    def _go_to(self, path: str) -> None:
        panel = self._active_panel()
        panel.path = path
        panel.marked.clear()
        panel.refresh_listing()

    # ------------------------------------------------- network locations
    def _add_network(self) -> None:
        if not netlocations.gio_available():
            self.app.push_screen(MessageScreen("Network locations need `gio` (GVFS) installed."))
            return

        def on_host(scheme: str, host: str | None) -> None:
            if not host or not host.strip():
                return
            if scheme == "smb":
                self._pick_share(host.strip())
            else:
                self._connect(netlocations.build_uri(scheme, host), save=True)

        def on_scheme(choice: str | None) -> None:
            if not choice:
                return
            scheme = choice.lower()
            self.app.push_screen(
                InputScreen(f"{choice} server (IP or name):"), lambda host: on_host(scheme, host)
            )

        self.app.push_screen(ChoiceScreen("Protocol", ["SMB", "SFTP", "FTP"]), on_scheme)

    def _pick_share(self, host: str, retried: bool = False) -> None:
        self.notify(f"Listing shares on {host}...")

        def work() -> None:
            shares, err = netlocations.list_shares(host)
            self.app.call_from_thread(done, shares, err)

        def done(shares: list[str] | None, err: str) -> None:
            if shares is None:
                if retried:
                    self.app.push_screen(MessageScreen(f"Could not list shares on {host}.\n{err}"))
                    return
                # Most likely a login is needed: let gio ask for it on the real
                # terminal (and offer to remember it in the keyring).
                with self.app.suspend():
                    netlocations.mount(netlocations.build_uri("smb", host), interactive=True)
                self._pick_share(host, retried=True)
                return
            if not shares:
                self.app.push_screen(MessageScreen(f"No shares found on {host}."))
                return
            self.app.push_screen(
                ChoiceScreen(f"Shares on {host}", shares),
                lambda share: share and self._connect(netlocations.build_uri("smb", host, share), save=True),
            )

        self.run_worker(work, thread=True)

    def _connect(self, uri: str, save: bool = False, retried: bool = False) -> None:
        self.notify(f"Connecting to {uri} ...")

        def work() -> None:
            ok, err = netlocations.mount(uri)
            path = netlocations.local_path(uri) if ok else None
            self.app.call_from_thread(done, path, err)

        def done(path: str | None, err: str) -> None:
            if path:
                if save:
                    netlocations.save_location(uri)
                self._go_to(path)
                return
            if retried:
                self.app.push_screen(MessageScreen(f"Could not connect to {uri}.\n{err}"))
                return
            with self.app.suspend():
                netlocations.mount(uri, interactive=True)
            self._connect(uri, save=save, retried=True)

        self.run_worker(work, thread=True)
