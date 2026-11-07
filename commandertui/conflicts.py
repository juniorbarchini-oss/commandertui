"""Read-only copy/move collision detection and pure per-entry decisions.

Identity is metadata-based, not a content comparison: SMB/GVFS can report
misleading sizes/mtimes. As in diff.py, equal sizes and mtimes within two
seconds count as identical; size differences still count as conflicts.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Literal

from .diff import MTIME_TOLERANCE_SECONDS
from .models import EntryKind, FileEntry, OpKind
from .scanner import scan_directory


@dataclass(frozen=True)
class Conflict:
    rel_path: str
    name: str
    src: FileEntry
    dst: FileEntry
    kind_mismatch: bool
    newer: Literal["src", "dst", "same"]


@dataclass
class ConflictReport:
    conflicts: list[Conflict] = field(default_factory=list)
    identical: int = 0
    dir_collisions: list[str] = field(default_factory=list)
    total_src_files_new: int = 0

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


class Resolution(str, Enum):
    OVERWRITE = "overwrite"
    SKIP = "skip"
    ONLY_NEWER = "only_newer"
    RENAME = "rename"
    MERGE = "merge"
    CANCEL = "cancel"


def allowed_resolutions(kind: OpKind) -> list[Resolution]:
    if kind == OpKind.COPY:
        return list(Resolution)
    if kind == OpKind.MOVE:
        return [Resolution.OVERWRITE, Resolution.SKIP, Resolution.RENAME, Resolution.CANCEL]
    raise ValueError(f"Unsupported operation: {kind}")


def rename_target(path: str) -> str:
    """Suggest a free sibling (not reserved); even dangling symlinks occupy names."""
    stem, extension = os.path.splitext(path)
    index = 1
    while True:
        candidate = f"{stem}_{index}{extension}"
        if not os.path.lexists(candidate):
            return candidate
        index += 1


def resolve(conflict: Conflict, resolution: Resolution) -> Literal["copy", "skip", "rename"]:
    """Choose an action without touching either side, including for MOVE skips.

    MERGE is a tree-level policy: missing files copy directly, while collisions
    need another per-file resolution. CANCEL must be handled by the caller.
    Neither is a valid per-conflict decision here.
    """
    if resolution == Resolution.OVERWRITE:
        return "copy"
    if resolution == Resolution.SKIP:
        return "skip"
    if resolution == Resolution.RENAME:
        return "rename"
    if resolution == Resolution.ONLY_NEWER:
        return "copy" if conflict.newer == "src" else "skip"
    raise ValueError(f"{resolution} requires caller-level handling")


def _entry(path: str, rel_path: str) -> FileEntry:
    metadata = os.lstat(path)
    kind = (
        EntryKind.SYMLINK
        if stat.S_ISLNK(metadata.st_mode)
        else EntryKind.DIR if stat.S_ISDIR(metadata.st_mode) else EntryKind.FILE
    )
    return FileEntry(
        rel_path,
        os.path.basename(rel_path),
        kind,
        0 if kind == EntryKind.DIR else metadata.st_size,
        metadata.st_mtime,
    )


def detect_conflicts(sources: list[str], dst_dir: str, cancel_check=None) -> ConflictReport:
    """Return collisions with paths relative to dst_dir, including source basename.

    Uses scanner exclusions and never follows symlinks. Directory/type collisions
    block their subtree, so descendants are not reported as independently new.
    Cancellation returns an empty report rather than a misleading partial result.
    """
    report = ConflictReport()

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    def compare(src: FileEntry, dst: FileEntry) -> None:
        mismatch = src.kind != dst.kind
        delta = src.mtime - dst.mtime
        newer = "same" if abs(delta) <= MTIME_TOLERANCE_SECONDS else "src" if delta > 0 else "dst"
        if not mismatch and src.size == dst.size and newer == "same":
            report.identical += 1
        else:
            report.conflicts.append(Conflict(src.rel_path, src.name, src, dst, mismatch, newer))

    for source in sources:
        if cancelled():
            return ConflictReport()
        name = os.path.basename(os.path.normpath(source))
        src = _entry(source, name)
        target = os.path.join(dst_dir, name)
        dst = _entry(target, name) if os.path.lexists(target) else None
        if src.kind != EntryKind.DIR:
            if dst is None:
                report.total_src_files_new += 1
            else:
                compare(src, dst)
            continue
        if dst is not None and dst.kind != EntryKind.DIR:
            compare(src, dst)
            continue
        left = scan_directory(source, cancel_check=cancel_check)
        if cancelled():
            return ConflictReport()
        if dst is None:
            report.total_src_files_new += sum(e.kind != EntryKind.DIR for e in left.values())
            continue
        report.dir_collisions.append(name)
        right = scan_directory(target, cancel_check=cancel_check)
        if cancelled():
            return ConflictReport()
        blocked: set[str] = set()
        for path, entry in left.items():
            if cancelled():
                return ConflictReport()
            if any(parent in blocked for parent in _parents(path)):
                continue
            other = right.get(path)
            if other is None:
                report.total_src_files_new += entry.kind != EntryKind.DIR
            elif entry.kind != EntryKind.DIR or other.kind != EntryKind.DIR:
                compare(replace(entry, rel_path=f"{name}/{path}"), replace(other, rel_path=f"{name}/{path}"))
                if entry.kind == EntryKind.DIR:
                    blocked.add(path)
    return report


def _parents(path: str):
    parent = os.path.dirname(path)
    while parent:
        yield parent
        parent = os.path.dirname(parent)
