from __future__ import annotations

import os

from textual.app import App

from . import settings, theme, updater
from .boot import BootScreen
from .main_screen import MainScreen


class CommanderApp(App):
    """Owns only the screen stack. All pane behavior lives in MainScreen --
    see its docstring for why that split matters for key bindings.
    """

    CSS = theme.CSS

    def __init__(
        self, left_path: str | None = None, right_path: str | None = None, check_updates: bool = True
    ) -> None:
        super().__init__()
        for phosphor in theme.PHOSPHOR_THEMES:
            self.register_theme(phosphor)
        saved = settings.load_theme()
        self.theme = saved if saved in self.available_themes else theme.DEFAULT_THEME
        self.check_updates = check_updates and not updater.updates_disabled()
        self.update_info: updater.UpdateInfo | None = None
        self.update_prompted = False
        self.left_path = os.path.abspath(left_path or os.path.expanduser("~"))
        self.right_path = os.path.abspath(right_path or os.path.expanduser("~"))

    def on_unmount(self) -> None:
        # Whatever theme is showing when the app closes is the one to reopen
        # with -- covers the picker (t) and Textual's own palette (ctrl+p).
        settings.save_theme(self.theme)

    def preview_theme(self, name: str) -> None:
        self.theme = name

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self.left_path, self.right_path))
        self.push_screen(BootScreen(), self._after_boot)

    def _after_boot(self, _result=None) -> None:
        # Looked up only once the boot animation is over, so a quick answer
        # can never pop a dialog over it.
        if self.check_updates:
            self.run_worker(self._look_for_update, thread=True)

    def _look_for_update(self) -> None:
        info = updater.check()
        if info:
            self.call_from_thread(self.update_found, info)

    def update_found(self, info: updater.UpdateInfo) -> None:
        """Remember the newer version and say so in the header; the dialog
        itself waits until the main screen is actually showing (not the boot
        animation)."""
        self.update_info = info
        self.sub_title = f"update {info.latest} available (press u)"
        if isinstance(self.screen, MainScreen):
            self.screen.prompt_update()

    def clear_update(self) -> None:
        self.update_info = None
        self.sub_title = ""

    # Convenience passthroughs so existing tests/tools that poke at the app
    # directly (e.g. app._panel("left")) keep working without knowing about
    # the MainScreen split.
    def _panel(self, side: str):
        return self.main_screen._panel(side)

    @property
    def main_screen(self) -> MainScreen:
        return next(s for s in self.screen_stack if isinstance(s, MainScreen))

    @property
    def active_side(self) -> str:
        return self.main_screen.active_side
