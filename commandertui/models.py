from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DiffStatus(str, Enum):
    LEFT_ONLY = "left_only"
    RIGHT_ONLY = "right_only"
    MODIFIED = "modified"
    IDENTICAL = "identical"


class EntryKind(str, Enum):
    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"


@dataclass(frozen=True)
class FileEntry:
    rel_path: str
    name: str
    kind: EntryKind
    size: int
    mtime: float


@dataclass(frozen=True)
class DiffEntry:
    rel_path: str
    name: str
    status: DiffStatus
    left: FileEntry | None
    right: FileEntry | None


class OpKind(str, Enum):
    COPY = "copy"
    MOVE = "move"
    DELETE = "delete"


class OpSide(str, Enum):
    LEFT_TO_RIGHT = "left_to_right"
    RIGHT_TO_LEFT = "right_to_left"
    LEFT = "left"
    RIGHT = "right"


@dataclass
class QueuedOp:
    kind: OpKind
    side: OpSide
    rel_path: str
    is_dir: bool
    size: int = 0


@dataclass
class OperationQueue:
    ops: list[QueuedOp] = field(default_factory=list)

    def add(self, op: QueuedOp) -> None:
        self.ops.append(op)

    def clear(self) -> None:
        self.ops.clear()

    def summary(self) -> dict:
        by_kind: dict[str, int] = {}
        total_bytes = 0
        for op in self.ops:
            by_kind[op.kind.value] = by_kind.get(op.kind.value, 0) + 1
            total_bytes += op.size
        return {"count": len(self.ops), "by_kind": by_kind, "total_bytes": total_bytes}

    def __len__(self) -> int:
        return len(self.ops)
