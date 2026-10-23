from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from .ascii_font import banner

CURSOR = "█"  # solid block, like a CRT text cursor
SPINNER = "|/-\\"
LINE_WIDTH = 44  # fixed, so a status changing later never moves the box
WIDE_ENOUGH = 114  # terminal columns for the double-size title (110 wide with its box)


def _line(label: str, status: str) -> str:
    dots = "." * (LINE_WIDTH - len(label) - len(status) - 2)
    return f"{label} {dots} {status}"


def _build_art(scale: int = 1, update_status: str = "OK") -> str:
    title_lines = banner("COMMANDER", scale=scale).split("\n")
    body_lines = [
        "",
        "T U I  --  dual-pane file manager",
        "",
        _line("CONNECT 2400", "OK"),
        _line("LOADING PANELS", "OK"),
        _line("CHECKING UPDATES", update_status),
        "READY.",
    ]
    content = title_lines + body_lines
    width = max(len(line) for line in content) + 2  # 1 space padding each side

    def pad(line: str) -> str:
        return f"|{line.center(width)}|"

    border = "+" + "-" * width + "+"
    boxed = [border] + [pad(line) for line in content] + [border]
    return "\n".join(boxed)


def update_status_text(state: str, latest: str = "") -> str:
    """What the CHECKING UPDATES line says for an app update state."""
    return {"ok": "OK", "off": "OFF", "offline": "OFFLINE", "skipped": "SKIPPED"}.get(state, f"NEW {latest}")


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
        art: str | None = None,
        chars_per_tick: int = 3,
        tick_seconds: float = 0.035,
        bell: bool = True,
    ) -> None:
        super().__init__()
        self.fixed_art = art  # tests/tools can pass a fixed picture; normally built live
        self.art = art or ART
        self._scale = 1
        self._frame = 0
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
        # Double-size title when the window is wide enough for it.
        self._scale = 2 if self.app.size.width >= WIDE_ENOUGH else 1
        self._timer = self.set_interval(self.tick_seconds, self._tick)

    def _pending(self) -> bool:
        return self.fixed_art is None and getattr(self.app, "update_state", "off") == "pending"

    def _current_art(self) -> str:
        if self.fixed_art is not None:
            return self.fixed_art
        state = getattr(self.app, "update_state", "off")
        if state == "pending":
            status = SPINNER[self._frame % 4]
        else:
            status = update_status_text(state, getattr(self.app, "update_latest", ""))
        return _build_art(self._scale, status)

    def _limit(self) -> int:
        """How far the reveal may go: while the update check is still running
        it waits just before the READY. row."""
        if not self._pending():
            return len(self.art)
        ready = self.art.index("READY.")
        return self.art.rfind("\n", 0, ready) + 1

    def _tick(self) -> None:
        self._frame += 1
        self.art = self._current_art()
        self._pos = min(self._limit(), self._pos + self.chars_per_tick)
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
            self.query_one("#boot-hint", Static).update("\\[ENTER] to continue")

    def action_continue_(self) -> None:
        if self._pos < self._limit():
            # Not drawn yet: Enter fast-forwards as far as it can go instead
            # of doing nothing, in case someone doesn't want to wait.
            self._pos = self._limit()
            self._tick()
            return
        if self._pending() and self._pos < len(self.art):
            # Reveal is parked waiting on the update check: don't make the
            # user wait for the network; a newer version still gets offered
            # when it arrives.
            self.dismiss()
            return
        self.dismiss()
