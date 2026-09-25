from __future__ import annotations

import os
import stat

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
