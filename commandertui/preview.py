"""macOS Quick Look file preview using `qlmanage -p`."""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
import time

_current_process: subprocess.Popen | None = None
_current_path: str | None = None


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_active() -> bool:
    """Return True if a preview process is currently running."""
    global _current_process, _current_path
    if _current_process is None:
        return False
    if _current_process.poll() is not None:
        _current_process = None
        _current_path = None
        return False
    return True


def current_path() -> str | None:
    """Return the absolute path of the file currently being previewed, if any."""
    if is_active():
        return _current_path
    return None


def close_preview() -> None:
    """Terminate the active Quick Look preview process, if any."""
    global _current_process, _current_path
    if _current_process is not None:
        if _current_process.poll() is None:
            try:
                _current_process.terminate()
                try:
                    _current_process.wait(timeout=0.5)
                except (subprocess.TimeoutExpired, OSError):
                    _current_process.kill()
            except OSError:
                pass
        _current_process = None
        _current_path = None


atexit.register(close_preview)


def _bring_to_front_worker(proc: subprocess.Popen) -> None:
    """Wait for qlmanage to appear in System Events and bring its window to front."""
    script = 'tell application "System Events" to set frontmost of (first process whose name is "qlmanage") to true'
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if proc.poll() is not None or _current_process is not proc:
            return
        try:
            res = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3.0,
            )
            if res.returncode == 0:
                return
        except Exception:
            return
        time.sleep(0.1)


def _bring_to_front(proc: subprocess.Popen) -> threading.Thread:
    thread = threading.Thread(target=_bring_to_front_worker, args=(proc,), daemon=True)
    thread.start()
    return thread


def toggle_preview(path: str) -> bool:
    """Toggle Quick Look preview for `path`.

    If preview for `path` is already open, closes it.
    If preview for another file is open, closes it and opens `path`.
    Returns True if preview was started, False if it was closed or cannot run.
    """
    global _current_process, _current_path
    if not is_macos():
        return False

    abs_path = os.path.abspath(path)

    # If already open for this exact file, close it
    if is_active() and _current_path == abs_path:
        close_preview()
        return False

    # Close previous file preview if different
    close_preview()

    if not os.path.isfile(abs_path):
        return False

    try:
        proc = subprocess.Popen(
            ["qlmanage", "-p", abs_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _current_process = proc
        _current_path = abs_path
        _bring_to_front(proc)
        return True
    except OSError:
        _current_process = None
        _current_path = None
        return False
