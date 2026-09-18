from __future__ import annotations

import os

from .models import EntryKind, FileEntry

DEFAULT_EXCLUSIONS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
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
