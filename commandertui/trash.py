from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _fallback_trash_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/.Trash")
    return os.path.expanduser("~/.local/share/Trash/files")


def send_to_trash(path: str) -> bool:
    """Move a file or directory to the system trash. Never a raw rm/rmtree.

    Three-layer fallback, in order of preference:
      1. send2trash (FreeDesktop-native, cross-desktop)
      2. `gio trash` (GLib/GIO, present on GNOME/KDE)
      3. Direct move into ~/.Trash (macOS) or ~/.local/share/Trash/files (Linux)
         as last resort (e.g. external disks or network mounts without native trash)
    """
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return False

    try:
        import send2trash

        send2trash.send2trash(abs_path)
        return True
    except Exception:
        pass

    try:
        result = subprocess.run(["gio", "trash", abs_path], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return True
    except Exception:
        pass

    try:
        trash_dir = _fallback_trash_dir()
        os.makedirs(trash_dir, exist_ok=True)
        base = os.path.basename(abs_path)
        name, ext = os.path.splitext(base)
        target = os.path.join(trash_dir, base)
        counter = 1
        while os.path.exists(target):
            target = os.path.join(trash_dir, f"{name}_{counter}{ext}")
            counter += 1
        shutil.move(abs_path, target)
        return True
    except Exception:
        return False
