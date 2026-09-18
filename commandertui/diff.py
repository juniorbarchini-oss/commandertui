from __future__ import annotations

import os

from .models import DiffEntry, DiffStatus, EntryKind
from .scanner import DEFAULT_EXCLUSIONS, scan_directory

# Files that differ by mtime alone (common after a plain copy that touched
# metadata) but are otherwise identical should not be flagged as modified.
MTIME_TOLERANCE_SECONDS = 2.0


def compare(
    left_root: str,
    right_root: str,
    exclusions: set[str] = frozenset(DEFAULT_EXCLUSIONS),
    cancel_check=None,
) -> list[DiffEntry]:
    if not os.path.isdir(left_root) or not os.path.isdir(right_root):
        raise FileNotFoundError("One or both paths do not exist")

    left = scan_directory(left_root, exclusions, cancel_check)
    if cancel_check and cancel_check():
        return []
    right = scan_directory(right_root, exclusions, cancel_check)
    if cancel_check and cancel_check():
        return []

    all_paths = sorted(set(left) | set(right))
    diffs: list[DiffEntry] = []

    for rel_path in all_paths:
        l = left.get(rel_path)
        r = right.get(rel_path)
        name = (l or r).name

        if l and not r:
            status = DiffStatus.LEFT_ONLY
        elif r and not l:
            status = DiffStatus.RIGHT_ONLY
        elif l.kind == EntryKind.DIR and r.kind == EntryKind.DIR:
            status = DiffStatus.IDENTICAL
        elif l.kind != r.kind:
            status = DiffStatus.MODIFIED
        elif l.size != r.size:
            status = DiffStatus.MODIFIED
        elif abs(l.mtime - r.mtime) > MTIME_TOLERANCE_SECONDS:
            status = DiffStatus.MODIFIED
        else:
            status = DiffStatus.IDENTICAL

        diffs.append(DiffEntry(rel_path=rel_path, name=name, status=status, left=l, right=r))

    return diffs


def summarize(diffs: list[DiffEntry]) -> dict:
    counts = {status: 0 for status in DiffStatus}
    for d in diffs:
        counts[d.status] += 1
    return {status.value: n for status, n in counts.items()}
