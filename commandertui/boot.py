from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from .ascii_font import banner

CURSOR = "█"  # solid block, like a CRT text cursor


def _build_art() -> str:
    title_lines = banner("COMMANDER").split("\n")
    body_lines = [
        "",
        "T U I  --  dual-pane file manager",
        "",
        "CONNECT 2400 ...................... OK",
        "LOADING PANELS ..................... OK",
        "READY.",
    ]
    content = title_lines + body_lines
    width = max(len(line) for line in content) + 2  # 1 space padding each side

    def pad(line: str) -> str:
        return f"|{line.center(width)}|"

    border = "+" + "-" * width + "+"
    boxed = [border] + [pad(line) for line in content] + [border]
    return "\n".join(boxed)


ART = _build_art()


class BootScreen(Screen):
    """A brief modem-style 'printing' reveal shown once at startup.

    Purely cosmetic: it reveals ART character by character with a visible
    cursor, like text arriving over a slow serial line. Once fully drawn it
    waits on screen for an explicit [Enter] rather than auto-dismissing --
    the point is to actually see the drawing happen, not blink and miss it.
    """

    DEFAULT_CSS = """
    BootScreen {
        align: center middle;
    }
    #boot-text {
        width: auto;
        height: auto;
    }
    #boot-hint {
        width: auto;
        height: auto;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("enter", "continue_", "Continue", priority=True)]

    def __init__(
        self,
        art: str = ART,
        chars_per_tick: int = 3,
        tick_seconds: float = 0.035,
        bell: bool = True,
    ) -> None:
        super().__init__()
        self.art = art
        self.chars_per_tick = chars_per_tick
        self.tick_seconds = tick_seconds
        self.bell = bell
        self._pos = 0
        self._timer = None
        self._done = False

    def compose(self) -> ComposeResult:
        # No Center/Middle wrapper containers: those default to width 100%,
        # which meant "align: center" had nothing to actually center -- the
        # boxed art rendered flush against the left edge instead. `width:
        # auto` on the Static itself (set in DEFAULT_CSS) is what lets the
        # screen's own `align: center middle` actually center it as a block.
        yield Static("", id="boot-text")
        yield Static("", id="boot-hint")

    def on_mount(self) -> None:
        self._timer = self.set_interval(self.tick_seconds, self._tick)

    def _tick(self) -> None:
        self._pos = min(len(self.art), self._pos + self.chars_per_tick)
        widget = self.query_one("#boot-text", Static)
        visible = self.art[: self._pos]
        cursor = CURSOR if self._pos < len(self.art) else ""
        widget.update(visible + cursor)

        if self._pos >= len(self.art):
            self._done = True
            if self._timer is not None:
                self._timer.stop()
            if self.bell:
                self.app.bell()
            self.query_one("#boot-hint", Static).update("[ENTER] to continue")

    def action_continue_(self) -> None:
        if not self._done:
            # Not drawn yet: Enter fast-forwards straight to the final frame
            # instead of doing nothing, in case someone doesn't want to wait.
            self._pos = len(self.art)
            self._tick()
            return
        self.dismiss()
