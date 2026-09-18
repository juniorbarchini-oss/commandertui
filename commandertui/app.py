from __future__ import annotations

import os

from textual.app import App

from .boot import BootScreen
from .main_screen import MainScreen
from .theme import css_for


class CommanderApp(App):
    """Owns only the screen stack. All pane behavior lives in MainScreen --
    see its docstring for why that split matters for key bindings.
    """

    CSS = css_for("green")

    def __init__(self, left_path: str | None = None, right_path: str | None = None) -> None:
        super().__init__()
        self.left_path = os.path.abspath(left_path or os.path.expanduser("~"))
        self.right_path = os.path.abspath(right_path or os.path.expanduser("~"))

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self.left_path, self.right_path))
        self.push_screen(BootScreen())

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
