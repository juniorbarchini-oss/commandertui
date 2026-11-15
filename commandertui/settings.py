"""User settings that survive restarts (theme, sort order, preview mode)."""

from __future__ import annotations

import json
import os

from .bookmarks import CONFIG_DIR

SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
PREVIEW_MODES = ("integrated", "quicklook")
IMAGE_MODES = ("auto", "halfcell", "unicode")


def _load() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        parent = os.path.dirname(SETTINGS_FILE) or CONFIG_DIR
        os.makedirs(parent, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass  # a read-only home must never break the app


def load_theme() -> str | None:
    value = _load().get("theme")
    return value if isinstance(value, str) else None


def save_theme(name: str) -> None:
    data = _load()
    data["theme"] = name
    _save(data)


def load_sort(side: str) -> tuple[str, bool] | None:
    """Load (column, descending) for `side` ('left' or 'right')."""
    data = _load().get("sort", {})
    if isinstance(data, dict) and side in data:
        col, desc = data[side]
        return col, bool(desc)
    return None


def save_sort(side: str, col: str, desc: bool) -> None:
    data = _load()
    if "sort" not in data or not isinstance(data["sort"], dict):
        data["sort"] = {}
    data["sort"][side] = [col, bool(desc)]
    _save(data)


def load_preview_mode() -> str:
    """Return 'quicklook' or 'integrated' (default)."""
    value = _load().get("preview_mode")
    return value if isinstance(value, str) and value in PREVIEW_MODES else "integrated"


def save_preview_mode(mode: str) -> None:
    if mode not in PREVIEW_MODES:
        raise ValueError(f"Invalid preview mode: {mode}")
    data = _load()
    data["preview_mode"] = mode
    _save(data)


def is_herdr_session() -> bool:
    """Return True when running inside a Herdr terminal multiplexer session."""
    return bool(
        os.environ.get("HERDR_ENV")
        or os.environ.get("HERDR_PANE_ID")
        or os.environ.get("HERDR_SOCKET_PATH")
        or os.environ.get("TERM_PROGRAM", "").lower() == "herdr"
    )


def load_image_mode() -> str | None:
    """Return explicitly saved image render mode, or None if unset/invalid."""
    value = _load().get("image_mode")
    return value if isinstance(value, str) and value in IMAGE_MODES else None


def save_image_mode(mode: str) -> None:
    if mode not in IMAGE_MODES:
        raise ValueError(f"Invalid image mode: {mode}")
    data = _load()
    data["image_mode"] = mode
    _save(data)


def default_image_mode() -> str:
    """Return initial image mode: explicit user choice > Herdr halfcell > auto."""
    saved = load_image_mode()
    if saved is not None:
        return saved
    return "halfcell" if is_herdr_session() else "auto"
