from __future__ import annotations

# Phosphor palettes, consistent with the rest of the green-CRT TUI family
# (btrfs-restore-tui). Amber is the classic VT220 alternative for long
# sessions.
PALETTES = {
    "green": {
        "foreground": "#00FF66",
        "dim": "#0A5C2C",
        "background": "#001A0D",
        "accent": "#66FFB2",
        "danger": "#FF4444",
        "warning": "#FFD24C",
    },
    "amber": {
        "foreground": "#FFB000",
        "dim": "#5C3F00",
        "background": "#1A1000",
        "accent": "#FFD24C",
        "danger": "#FF4444",
        "warning": "#FFEE66",
    },
}


def css_for(palette_name: str = "green") -> str:
    p = PALETTES.get(palette_name, PALETTES["green"])
    return f"""
Screen {{
    background: {p['background']};
    color: {p['foreground']};
}}

.dim {{
    color: {p['dim']};
}}

.accent {{
    color: {p['accent']};
}}

.danger {{
    color: {p['danger']};
}}

.warning {{
    color: {p['warning']};
}}

Header, Footer {{
    background: {p['background']};
    color: {p['foreground']};
}}

/* DataTable draws its own cell colors independently of Screen, so every
   part of it needs to be told about the palette explicitly -- otherwise it
   falls back to Textual's default theme (white text) regardless of what the
   rest of the app looks like. */
DataTable {{
    background: {p['background']};
    color: {p['foreground']};
    scrollbar-color: {p['dim']};
    scrollbar-background: {p['background']};
}}

DataTable > .datatable--header {{
    background: {p['background']};
    color: {p['accent']};
    text-style: bold;
}}

DataTable > .datatable--cursor {{
    background: {p['foreground']};
    color: {p['background']};
}}

DataTable > .datatable--odd-row {{
    background: {p['background']};
}}

DataTable > .datatable--even-row {{
    background: {p['background']};
}}

PlacesScreen #places-box, ChoiceScreen #choice-box {{
    background: {p['background']};
    color: {p['foreground']};
    border: heavy {p['foreground']};
}}

ConfirmScreen #confirm-box {{
    background: {p['background']};
    color: {p['foreground']};
    border: heavy {p['foreground']};
}}

ConfirmScreen #confirm-box.danger {{
    color: {p['danger']};
    border: heavy {p['danger']};
}}

ConfirmScreen Button, ProgressScreen Button, PlacesScreen Button, ChoiceScreen Button {{
    background: {p['background']};
    color: {p['foreground']};
    border: tall {p['foreground']};
    text-style: none;
}}

ConfirmScreen Button.danger, ProgressScreen Button.danger {{
    color: {p['danger']};
    border: tall {p['danger']};
}}

ConfirmScreen Button:focus, ProgressScreen Button:focus, PlacesScreen Button:focus, ChoiceScreen Button:focus {{
    background: {p['foreground']};
    color: {p['background']};
    text-style: bold;
}}

ConfirmScreen Button.danger:focus, ProgressScreen Button.danger:focus {{
    background: {p['danger']};
    color: {p['background']};
}}

ChoiceScreen OptionList {{
    background: {p['background']};
    color: {p['foreground']};
    border: tall {p['dim']};
}}

ChoiceScreen OptionList:focus {{
    border: tall {p['foreground']};
}}

ChoiceScreen OptionList > .option-list--option-highlighted {{
    background: {p['foreground']};
    color: {p['background']};
    text-style: bold;
}}

FilePanel {{
    border: round {p['dim']};
}}

FilePanel.active-panel {{
    border: heavy {p['foreground']};
}}

DataTable:blur > .datatable--cursor {{
    background: {p['dim']};
    color: {p['background']};
}}

FunctionBar {{
    height: 3;
    background: {p['background']};
}}

FnButton {{
    background: {p['background']};
    color: {p['foreground']};
    border: round {p['foreground']};
    min-width: 0;
    width: auto;
    padding: 0;
    margin: 0;
    text-style: none;
}}

FnButton:hover {{
    background: {p['foreground']};
    color: {p['background']};
}}

.pathbar {{
    color: {p['dim']};
    padding: 0 1;
}}

.pathbar.active {{
    background: {p['foreground']};
    color: {p['background']};
    text-style: bold;
}}
"""
