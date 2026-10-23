"""Which text editor F4 opens: the user's own choice first."""

from __future__ import annotations

import os
import shlex
import shutil

FALLBACKS = ("nano", "vim", "vi")


def editor_command() -> list[str] | None:
    """$VISUAL, then $EDITOR, then the first of nano/vim/vi that is installed."""
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var, "").strip()
        if value:
            try:
                parts = shlex.split(value)
            except ValueError:
                continue
            if parts and shutil.which(parts[0]):
                return parts
    for name in FALLBACKS:
        if shutil.which(name):
            return [name]
    return None
