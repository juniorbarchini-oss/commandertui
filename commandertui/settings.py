"""User settings that survive restarts (today: the color theme)."""

from __future__ import annotations

import json
import os

from .bookmarks import CONFIG_DIR

SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")


def _load() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_theme() -> str | None:
    value = _load().get("theme")
    return value if isinstance(value, str) else None


def save_theme(name: str) -> None:
    data = _load()
    data["theme"] = name
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass  # a read-only home must never break the app
