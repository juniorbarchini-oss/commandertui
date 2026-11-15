from __future__ import annotations

import fnmatch
import os
import stat
from collections.abc import Callable

from .models import EntryKind, FileEntry

DEFAULT_EXCLUSIONS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _kind_of(entry: os.DirEntry) -> EntryKind:
    if entry.is_symlink():
        return EntryKind.SYMLINK
    if entry.is_dir(follow_symlinks=False):
        return EntryKind.DIR
    return EntryKind.FILE


def scan_directory(
    root: str,
    exclusions: set[str] = frozenset(DEFAULT_EXCLUSIONS),
    cancel_check=None,
) -> dict[str, FileEntry]:
    """Recursively scan a directory, returning a map of rel_path -> FileEntry.

    Directories are included as entries too (needed to diff empty folders),
    but only files/symlinks contribute to size comparisons.
    """
    results: dict[str, FileEntry] = {}

    def walk(current_abs: str, current_rel: str) -> None:
        if cancel_check and cancel_check():
            return
        try:
            with os.scandir(current_abs) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (PermissionError, FileNotFoundError):
            return

        for entry in entries:
            if entry.name in exclusions:
                continue
            rel_path = f"{current_rel}/{entry.name}" if current_rel else entry.name
            kind = _kind_of(entry)
            try:
                stat = entry.stat(follow_symlinks=False)
            except (PermissionError, FileNotFoundError):
                continue

            results[rel_path] = FileEntry(
                rel_path=rel_path,
                name=entry.name,
                kind=kind,
                size=stat.st_size if kind != EntryKind.DIR else 0,
                mtime=stat.st_mtime,
            )

            if kind == EntryKind.DIR:
                walk(entry.path, rel_path)

    walk(root, "")
    return results


def tree_stats(paths, cancel_check=None, on_progress=None) -> tuple[int, int, int]:
    """(files, subfolders, bytes) under `paths`. Read-only, never follows
    symlinks; the given top-level folders themselves aren't counted."""
    top = set(paths)
    stack = list(paths)
    files = dirs = total = 0
    while stack:
        if cancel_check and cancel_check():
            break
        p = stack.pop()
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            if p not in top:
                dirs += 1
            try:
                with os.scandir(p) as it:
                    stack.extend(e.path for e in it)
            except OSError:
                pass
        else:
            files += 1
            total += st.st_size
        if on_progress:
            on_progress(files, dirs, total)
    return files, dirs, total


def path_size(path: str) -> int:
    """Total bytes under `path` -- a single file's size, or a directory
    tree's total. Used for the byte counts shown in confirmation dialogs;
    without this, manually-marked items always reported "0 bytes" since
    only the bulk compare/sync path (plan.py) computed real sizes.
    """
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            return sum(path_size(entry.path) for entry in os.scandir(path))
        return os.path.getsize(path)
    except OSError:
        return 0


FIND_LIMIT = 500


def name_matcher(pattern: str) -> Callable[[str], bool]:
    """Case-insensitive match on a file name: a glob when the pattern has wildcards
    (`*.pdf`, `report-??.txt`), otherwise "contains this text"."""
    needle = pattern.strip().lower()
    if any(ch in needle for ch in "*?["):
        return lambda name: fnmatch.fnmatchcase(name.lower(), needle)
    return lambda name: needle in name.lower()


def find_names(
    root: str,
    pattern: str,
    cancelled: Callable[[], bool] = lambda: False,
    on_found: Callable[[str], None] | None = None,
    *,
    show_hidden: bool = False,
    limit: int = FIND_LIMIT,
) -> tuple[list[str], bool]:
    """Walk `root` looking for files and folders whose name matches `pattern`.

    Returns (paths, truncated). Only reads directory listings; symlinked folders are
    reported but not entered, so a link loop cannot trap the walk. Unreadable folders
    are skipped. `cancelled` is checked for every directory and entry.
    """
    matches = name_matcher(pattern)
    found: list[str] = []
    if not pattern.strip():
        return found, False
    stack = [root]
    while stack:
        if cancelled():
            return found, False
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue
        subdirs: list[str] = []
        for entry in entries:
            if cancelled():
                return found, False
            if not show_hidden and entry.name.startswith("."):
                continue
            if matches(entry.name):
                if len(found) >= limit:
                    return found, True
                found.append(entry.path)
                if on_found is not None:
                    on_found(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
            except OSError:
                continue
        stack.extend(reversed(subdirs))
    return found, False
