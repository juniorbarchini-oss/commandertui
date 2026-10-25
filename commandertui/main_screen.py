from __future__ import annotations

import os
import subprocess

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Header

from .compare_screen import CompareScreen
from . import __version__, editor, netlocations, preview, theme, updater
from .modals import (
    ADD_NETWORK,
    CHANGE_LOGIN,
    ChoiceScreen,
    FilterScreen,
    InputScreen,
    MessageScreen,
    PlacesScreen,
    SizeScreen,
    UpdateScreen,
)
from .models import OpKind, OpSide, OperationQueue, QueuedOp
from .run_queue import run_queue_with_progress
from .scanner import path_size
from .widgets import PARENT_MARK, FilePanel, FunctionBar, MainTopBar

LOGIN_ANON = "Anonymous (no login)"
LOGIN_USER = "User and password"


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
        Binding("up", "nav_up", "Up", priority=True),
        Binding("down", "nav_down", "Down", priority=True),
        Binding("left", "nav_left", "Left", priority=True),
        Binding("right", "nav_right", "Right", priority=True),
        Binding("backspace", "up_dir", "Up", priority=True),
        Binding("space", "toggle_mark", "Mark", priority=True),
        Binding("t", "pick_theme", "Theme", priority=True),
        Binding("u", "update_app", "Update", priority=True),
        Binding("slash", "filter", "Filter", priority=True),
        Binding("plus", "mark_pattern", "Mark pattern", priority=True),
        Binding("minus", "unmark_pattern", "Unmark pattern", priority=True),
        Binding("asterisk", "invert_marks", "Invert marks", priority=True),
        Binding("f2", "rename", "Rename", priority=True),
        Binding("f4", "edit", "Edit", priority=True),
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
        Binding("v", "preview_file", "Preview", priority=True),
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

    def on_screen_resume(self) -> None:
        # First time the main screen shows (the boot animation just closed):
        # tell the user about an update the background check already found.
        self.prompt_update()

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
        panel = self._active_panel()
        if panel.header_active:
            panel.header_active = False
            panel.update_header_labels()
        self._set_active("right" if self.active_side == "left" else "left")

    def action_enter_dir(self) -> None:
        panel = self._active_panel()
        if panel.header_active:
            col_name = ["Name", "Size", "Modified"][panel.header_selected_index]
            panel.toggle_sort(col_name)
        else:
            panel.enter_selected()

    def action_nav_up(self) -> None:
        panel = self._active_panel()
        table = panel.query_one(f"#{panel.side}-table", DataTable)
        if panel.header_active:
            return
        if table.cursor_row == 0:
            panel.header_active = True
            panel.update_header_labels()
        else:
            table.action_cursor_up()

    def action_nav_down(self) -> None:
        panel = self._active_panel()
        table = panel.query_one(f"#{panel.side}-table", DataTable)
        if panel.header_active:
            panel.header_active = False
            panel.update_header_labels()
        else:
            table.action_cursor_down()

    def action_nav_left(self) -> None:
        panel = self._active_panel()
        if panel.header_active:
            panel.header_selected_index = max(0, panel.header_selected_index - 1)
            panel.update_header_labels()
        else:
            table = panel.query_one(f"#{panel.side}-table", DataTable)
            table.action_cursor_left()

    def action_nav_right(self) -> None:
        panel = self._active_panel()
        if panel.header_active:
            panel.header_selected_index = min(2, panel.header_selected_index + 1)
            panel.update_header_labels()
        else:
            table = panel.query_one(f"#{panel.side}-table", DataTable)
            table.action_cursor_right()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        # Col 0: " ", Col 1: Name, Col 2: Size, Col 3: Modified (Date)
        if event.column_index < 1:
            return
        col_names = {1: "Name", 2: "Size", 3: "Modified"}
        col_name = col_names.get(event.column_index)
        if not col_name:
            return
        panel = None
        if isinstance(event.data_table.parent, FilePanel):
            panel = event.data_table.parent
        elif event.data_table.id and "left" in event.data_table.id:
            panel = self._panel("left")
        elif event.data_table.id and "right" in event.data_table.id:
            panel = self._panel("right")
        else:
            panel = self._active_panel()

        if self.active_side != panel.side:
            active = self._active_panel()
            if active.header_active:
                active.header_active = False
                active.update_header_labels()
            self._set_active(panel.side)

        panel.header_selected_index = event.column_index - 1
        panel.toggle_sort(col_name)

    def action_up_dir(self) -> None:
        panel = self._active_panel()
        if panel.header_active:
            panel.header_active = False
            panel.update_header_labels()
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
    SELECT_MENU = ["Mark by pattern  (+)", "Unmark by pattern  (-)", "Invert marks  (*)"]

    def action_select_menu(self) -> None:
        def on_choice(choice: str | None) -> None:
            if choice == self.SELECT_MENU[0]:
                self.action_mark_pattern()
            elif choice == self.SELECT_MENU[1]:
                self.action_unmark_pattern()
            elif choice == self.SELECT_MENU[2]:
                self.action_invert_marks()

        self.app.push_screen(ChoiceScreen("Select", self.SELECT_MENU), on_choice)

    def action_mark_pattern(self) -> None:
        panel = self._active_panel()

        def on_pattern(pattern: str | None) -> None:
            if pattern:
                count = panel.mark_matching(pattern)
                self.notify(f"Marked {count} item(s)." if count else "Nothing matches.")

        self.app.push_screen(InputScreen("Mark items matching (e.g. *.iso, or plain text):"), on_pattern)

    def action_unmark_pattern(self) -> None:
        panel = self._active_panel()

        def on_pattern(pattern: str | None) -> None:
            if pattern:
                count = panel.unmark_matching(pattern)
                self.notify(f"Unmarked {count} item(s)." if count else "No marked item matches.")

        self.app.push_screen(InputScreen("Unmark items matching (e.g. *.iso, or plain text):"), on_pattern)

    def action_invert_marks(self) -> None:
        self._active_panel().invert_marks()

    # ------------------------------------------------------------ updates
    def action_update_app(self) -> None:
        if self.app.update_info is not None:
            self.app.update_prompted = False
            self.prompt_update()
            return
        # Nothing known yet: ask PyPI right now instead of trusting the daily check.
        self.notify("Checking for updates...")
        self.run_worker(self._check_now, thread=True)

    def _check_now(self) -> None:
        info, reachable = updater.check_now()
        self.app.call_from_thread(self._check_done, info, reachable)

    def _check_done(self, info, reachable: bool) -> None:
        if info is not None:
            self.app.update_prompted = False
            self.app.update_found(info)
        elif not reachable:
            self.notify("Could not reach PyPI. Check the connection and try again.", severity="warning")
        else:
            self.notify(f"CommanderTUI {__version__} is up to date.")

    def prompt_update(self) -> None:
        info = self.app.update_info
        if info is None or self.app.update_prompted:
            return
        if self.app.screen is not self:
            return  # the boot animation (or a dialog) is on top: resume will call us again
        self.app.update_prompted = True

        def on_choice(choice: str | None) -> None:
            if choice == "skip":
                updater.skip_version(info.latest)
                self.app.clear_update()
            elif choice == "now":
                with self.app.suspend():
                    ok = updater.run_update(info)
                if ok:
                    self.app.clear_update()
                    self.app.push_screen(
                        MessageScreen(f"Updated to {info.latest}.\nQuit and reopen CommanderTUI to use it.")
                    )
                else:
                    self.app.push_screen(
                        MessageScreen(
                            "The update did not finish. The package index may not have the new version yet:\nwait a few minutes and try again, or check the terminal output."
                        )
                    )

        self.app.push_screen(UpdateScreen(info), on_choice)

    def action_pick_theme(self) -> None:
        labels = [label for label, _ in theme.THEME_CHOICES]
        names = [name for _, name in theme.THEME_CHOICES]
        original = self.app.theme
        initial = names.index(original) if original in names else 0

        def on_choice(choice: str | None) -> None:
            if choice is None:
                self.app.preview_theme(original)  # Esc: back to what it was
            else:
                self.app.theme = names[labels.index(choice)]  # saved by the app

        self.app.push_screen(
            ChoiceScreen(
                "Color theme",
                labels,
                initial=initial,
                on_highlight=lambda i: self.app.preview_theme(names[i]),
            ),
            on_choice,
        )

    def action_filter(self) -> None:
        panel = self._active_panel()
        self.app.push_screen(
            FilterScreen(panel.filter_text, panel.set_filter),
            lambda text: panel.set_filter(text or ""),
        )

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

    def action_edit(self) -> None:
        panel = self._active_panel()
        name = panel.selected_name()
        if not name or name == PARENT_MARK:
            self._nothing_selected()
            return
        path = os.path.join(panel.path, name)
        if os.path.isdir(path):
            self.app.push_screen(MessageScreen(f'"{name}" is a folder. Pick a file to edit.'))
            return
        command = editor.editor_command()
        if command is None:
            self.app.push_screen(MessageScreen("No text editor found.\nSet $EDITOR or install nano."))
            return
        with self.app.suspend():
            try:
                subprocess.run([*command, path])
            except OSError as e:
                error = str(e)
            else:
                error = ""
        if error:
            self.app.push_screen(MessageScreen(f"Could not start the editor:\n{error}"))
        panel.refresh_listing()
        panel.move_cursor_to(name)

    def action_preview_file(self) -> None:
        if not preview.is_macos():
            return
        panel = self._active_panel()
        name = panel.selected_name()
        if not name or name == PARENT_MARK:
            self.app.push_screen(MessageScreen("Preview works on files only"))
            return
        path = os.path.join(panel.path, name)
        if os.path.isdir(path):
            self.app.push_screen(MessageScreen("Preview works on files only"))
            return
        preview.toggle_preview(path)

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
            elif path.startswith(CHANGE_LOGIN):
                self._change_login(path[len(CHANGE_LOGIN) :])
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

        def on_user(host: str, user: str | None) -> None:
            if not user or not user.strip():
                return
            self._pick_share(host, user=user.strip())

        def on_host(scheme: str, host: str | None) -> None:
            if not host or not host.strip():
                return
            host_clean = host.strip()
            if scheme == "smb" and netlocations.is_macos():
                self.app.push_screen(InputScreen("SMB user:"), lambda u: on_user(host_clean, u))
            elif scheme in ("smb", "ftp"):
                self.app.push_screen(
                    ChoiceScreen(f"Login for {host_clean}", [LOGIN_ANON, LOGIN_USER]),
                    lambda choice: on_login(scheme, host_clean, choice),
                )
            else:
                self._connect(netlocations.build_uri(scheme, host_clean), save=True)

        def on_login(scheme: str, host: str, choice: str | None) -> None:
            if not choice:
                return
            anonymous = choice == LOGIN_ANON
            if scheme == "smb":
                self._pick_share(host, anonymous=anonymous, reset=not anonymous)
            else:
                self._connect(
                    netlocations.build_uri(scheme, host), save=True, anonymous=anonymous, reset=not anonymous
                )

        def on_scheme(choice: str | None) -> None:
            if not choice:
                return
            scheme = choice.lower()
            if netlocations.is_macos() and scheme != "smb":
                self.app.push_screen(MessageScreen(netlocations.MACOS_UNSUPPORTED_MSG))
                return
            if netlocations.backend_missing(scheme):
                self._offer_backend_install(scheme, choice)
                return
            self.app.push_screen(
                InputScreen(f"{choice} server (IP or name):"), lambda host: on_host(scheme, host)
            )

        self.app.push_screen(ChoiceScreen("Protocol", ["SMB", "SFTP", "FTP"]), on_scheme)

    def _change_login(self, uri: str) -> None:
        """Forget the session of a saved location and connect again, anonymous or with another user."""
        if netlocations.is_macos():
            self.app.push_screen(
                MessageScreen("Change login is not available on macOS: remove the location and add it again.")
            )
            return
        local = netlocations.local_path(uri)
        if local:
            for side in ("left", "right"):
                panel = self._panel(side)
                if panel.path == local or panel.path.startswith(local.rstrip(os.sep) + os.sep):
                    panel.path = os.path.expanduser("~")
                    panel.marked.clear()
                    panel.refresh_listing()

        def on_choice(choice: str | None) -> None:
            if not choice:
                return
            anonymous = choice == LOGIN_ANON
            self._connect(uri, save=True, anonymous=anonymous, reset=True)

        scheme = uri.partition("://")[0].lower()
        options = [LOGIN_ANON, LOGIN_USER] if scheme in ("smb", "ftp") else [LOGIN_USER]
        self.app.push_screen(ChoiceScreen(f"Login for {netlocations.label_for(uri)}", options), on_choice)

    def _offer_backend_install(self, scheme: str, label: str) -> None:
        command = netlocations.backend_install_command()
        if command is None:
            self.app.push_screen(
                MessageScreen(
                    f"{label} needs the GVFS {scheme} backend, which is not installed.\nInstall your system's GVFS backends package and try again."
                )
            )
            return
        text = " ".join(command)

        def on_choice(choice: str | None) -> None:
            if choice != "Install now":
                return
            with self.app.suspend():
                ok = netlocations.install_backend()
            if ok:
                self.app.push_screen(
                    MessageScreen(
                        "GVFS backends installed.\nQuit and reopen CommanderTUI, then add the location again."
                    )
                )
            else:
                self.app.push_screen(
                    MessageScreen("The install did not finish. Check the terminal output, then try again.")
                )

        self.app.push_screen(
            ChoiceScreen(
                f"{label} needs the GVFS backend, which is missing. Install it now? ({text}; asks for your sudo password)",
                ["Install now", "Cancel"],
            ),
            on_choice,
        )

    def _pick_share(
        self,
        host: str,
        retried: bool = False,
        user: str | None = None,
        anonymous: bool = False,
        reset: bool = False,
    ) -> None:
        self.notify(f"Listing shares on {host}...")

        def work() -> None:
            if reset:  # forget a previous session so the server asks for a login again
                netlocations.reset_login(netlocations.build_uri("smb", host))
            shares, err = netlocations.list_shares(host, user=user, anonymous=anonymous)
            self.app.call_from_thread(done, shares, err)

        def done(shares: list[str] | None, err: str) -> None:
            if shares is None:
                if retried or anonymous:
                    self.app.push_screen(MessageScreen(f"Could not list shares on {host}.\n{err}"))
                    return
                if netlocations.is_macos():
                    netlocations.mount(netlocations.build_uri("smb", host, user=user), interactive=True)
                else:
                    # Most likely a login is needed: let gio ask for it on the real
                    # terminal (and offer to remember it in the keyring).
                    with self.app.suspend():
                        netlocations.mount(netlocations.build_uri("smb", host), interactive=True)
                self._pick_share(host, retried=True, user=user)
                return
            if not shares:
                self.app.push_screen(MessageScreen(f"No shares found on {host}."))
                return
            self.app.push_screen(
                ChoiceScreen(f"Shares on {host}", shares),
                lambda share: share
                and self._connect(
                    netlocations.build_uri("smb", host, share, user=user), save=True, anonymous=anonymous
                ),
            )

        self.run_worker(work, thread=True)

    def _connect(
        self,
        uri: str,
        save: bool = False,
        retried: bool = False,
        anonymous: bool | None = None,
        reset: bool = False,
    ) -> None:
        if netlocations.is_macos() and not uri.lower().startswith("smb://"):
            self.app.push_screen(MessageScreen(netlocations.MACOS_UNSUPPORTED_MSG))
            return
        self.notify(f"Connecting to {uri} ...")
        anon = netlocations.is_anonymous(uri) if anonymous is None else anonymous

        def work() -> None:
            if reset:  # forget a previous session so the server asks for a login again
                netlocations.reset_login(uri)
            ok, err = netlocations.mount(uri, anonymous=anon)
            path = netlocations.local_path(uri) if ok else None
            self.app.call_from_thread(done, path, err)

        def done(path: str | None, err: str) -> None:
            if path:
                if save:
                    netlocations.save_location(uri, anonymous=anon)
                self._go_to(path)
                return
            if retried or anon or netlocations.is_macos():
                self.app.push_screen(MessageScreen(f"Could not connect to {uri}.\n{err}"))
                return
            with self.app.suspend():
                netlocations.mount(uri, interactive=True, anonymous=False)
            self._connect(uri, save=save, retried=True, anonymous=False)

        self.run_worker(work, thread=True)
