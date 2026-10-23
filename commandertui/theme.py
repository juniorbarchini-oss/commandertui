from __future__ import annotations

from textual.theme import Theme

# The two phosphor palettes (green CRT, amber VT220) are regular Textual
# themes, so they sit in the same list as the stock ones (nord, gruvbox...).
PHOSPHOR_THEMES = [
    Theme(
        name="phosphor-green",
        primary="#00FF66",
        secondary="#0A5C2C",
        accent="#66FFB2",
        warning="#FFD24C",
        error="#FF4444",
        success="#00FF66",
        foreground="#00FF66",
        background="#001A0D",
        surface="#001A0D",
        panel="#001A0D",
        dark=True,
    ),
    Theme(
        name="phosphor-amber",
        primary="#FFB000",
        secondary="#5C3F00",
        accent="#FFD24C",
        warning="#FFEE66",
        error="#FF4444",
        success="#FFB000",
        foreground="#FFB000",
        background="#1A1000",
        surface="#1A1000",
        panel="#1A1000",
        dark=True,
    ),
]

DEFAULT_THEME = "phosphor-green"

# (label shown in the picker, Textual theme name)
THEME_CHOICES = [
    ("Phosphor green", "phosphor-green"),
    ("Phosphor amber", "phosphor-amber"),
    ("Nord", "nord"),
    ("Gruvbox", "gruvbox"),
    ("Dracula", "dracula"),
    ("Tokyo Night", "tokyo-night"),
    ("Catppuccin Mocha", "catppuccin-mocha"),
    ("Solarized Dark", "solarized-dark"),
    ("Monokai", "monokai"),
    ("Textual Dark", "textual-dark"),
]

# Written with Textual's theme variables only ($primary = borders/highlights,
# $foreground = text, $error/$warning/$accent = meaning colors), so every
# theme above restyles the whole app.
CSS = """
Screen {
    background: $background;
    color: $foreground;
}

/* The filter bar floats over the panes so you watch the list shrink as you type. */
FilterScreen {
    background: transparent;
}

.dim {
    color: $foreground 40%;
}

.accent {
    color: $accent;
}

.danger {
    color: $error;
}

.warning {
    color: $warning;
}

Header, Footer {
    background: $background;
    color: $foreground;
}

/* DataTable draws its own cell colors independently of Screen, so every
   part of it needs to be told about the palette explicitly -- otherwise it
   falls back to Textual's default theme (white text) regardless of what the
   rest of the app looks like. */
DataTable {
    background: $background;
    color: $foreground;
    scrollbar-color: $foreground 40%;
    scrollbar-background: $background;
}

DataTable > .datatable--header {
    background: $background;
    color: $accent;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: $primary;
    color: $background;
}

DataTable > .datatable--odd-row {
    background: $background;
}

DataTable > .datatable--even-row {
    background: $background;
}

ConfirmScreen #confirm-box {
    background: $background;
    color: $foreground;
    border: heavy $primary;
}

ConfirmScreen #confirm-box.danger {
    color: $error;
    border: heavy $error;
}

ConfirmScreen Button, ProgressScreen Button, InputScreen Button, MessageScreen Button, SizeScreen Button, PlacesScreen Button, ChoiceScreen Button, FilterScreen Button, UpdateScreen Button {
    background: $background;
    color: $foreground;
    border: tall $primary;
    text-style: none;
}

ConfirmScreen Button.danger, ProgressScreen Button.danger {
    color: $error;
    border: tall $error;
}

ConfirmScreen Button:focus, ProgressScreen Button:focus, InputScreen Button:focus, MessageScreen Button:focus, SizeScreen Button:focus, PlacesScreen Button:focus, ChoiceScreen Button:focus, FilterScreen Button:focus, UpdateScreen Button:focus {
    background: $primary;
    color: $background;
    text-style: bold;
}

ConfirmScreen Button.danger:focus, ProgressScreen Button.danger:focus {
    background: $error;
    color: $background;
}

MessageScreen #msg-box, SizeScreen #size-box, PlacesScreen #places-box, ChoiceScreen #choice-box, FilterScreen #filter-box, UpdateScreen #update-box {
    background: $background;
    color: $foreground;
    border: heavy $primary;
}

InputScreen #input-box {
    background: $background;
    color: $foreground;
    border: heavy $primary;
}

InputScreen Input, FilterScreen Input {
    background: $background;
    color: $foreground;
    border: tall $foreground 40%;
}

InputScreen Input:focus, FilterScreen Input:focus {
    border: tall $primary;
}

InputScreen Input > .input--cursor, FilterScreen Input > .input--cursor {
    background: $primary;
    color: $background;
}

InputScreen Input > .input--selection, FilterScreen Input > .input--selection {
    background: $foreground 40%;
    color: $foreground;
}

ChoiceScreen OptionList {
    background: $background;
    color: $foreground;
    border: tall $foreground 40%;
}

ChoiceScreen OptionList:focus {
    border: tall $primary;
}

ChoiceScreen OptionList > .option-list--option-highlighted {
    background: $primary;
    color: $background;
    text-style: bold;
}

FilePanel {
    border: round $foreground 40%;
}

FilePanel.active-panel {
    border: heavy $primary;
}

DataTable:blur > .datatable--cursor {
    background: $foreground 40%;
    color: $background;
}

CompareBar {
    dock: bottom;
}

#bars {
    height: auto;
}

#bars.wide {
    layout: horizontal;
    height: 3;
}

#bars.narrow {
    layout: vertical;
    height: 6;
}

#bars.wide MainTopBar, #bars.wide FunctionBar {
    width: auto;
}

#bars.narrow MainTopBar, #bars.narrow FunctionBar {
    width: 100%;
}

MainTopBar, FunctionBar, CompareBar {
    height: 3;
    background: $background;
}

FnButton {
    background: $background;
    color: $foreground;
    border: round $primary;
    min-width: 0;
    width: auto;
    padding: 0;
    margin: 0;
    text-style: none;
}

FnButton:hover {
    background: $primary;
    color: $background;
}

.pathrow {
    height: 1;
}

.pathrow .pathbar {
    width: 1fr;
}

.pathrow .pathfree, .pathrow .pathsel {
    width: auto;
    padding: 0 2;
}

.pathbar {
    color: $foreground 40%;
    padding: 0 1;
}

.pathbar.active {
    background: $primary;
    color: $background;
    text-style: bold;
}
"""
