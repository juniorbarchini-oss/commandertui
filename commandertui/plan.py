from __future__ import annotations

from .models import DiffEntry, DiffStatus, EntryKind, OpKind, OpSide, OperationQueue, QueuedOp


def plan_mirror(diffs: list[DiffEntry]) -> OperationQueue:
    """Make Right identical to Left: copy everything Left has, delete everything
    Right has that Left doesn't."""
    queue = OperationQueue()
    for d in diffs:
        if d.status in (DiffStatus.LEFT_ONLY, DiffStatus.MODIFIED):
            queue.add(_copy_op(d, OpSide.LEFT_TO_RIGHT))
        elif d.status == DiffStatus.RIGHT_ONLY:
            queue.add(QueuedOp(OpKind.DELETE, OpSide.RIGHT, d.rel_path, _is_dir(d.right)))
    return queue


def plan_update(diffs: list[DiffEntry]) -> OperationQueue:
    """Copy only new/modified files from Left to Right. Never deletes."""
    queue = OperationQueue()
    for d in diffs:
        if d.status in (DiffStatus.LEFT_ONLY, DiffStatus.MODIFIED):
            queue.add(_copy_op(d, OpSide.LEFT_TO_RIGHT))
    return queue


def plan_two_way(diffs: list[DiffEntry]) -> tuple[OperationQueue, list[DiffEntry]]:
    """Propagate each side's exclusive files to the other. MODIFIED entries are
    real conflicts (both sides changed) and are returned separately for the
    user to resolve by hand rather than guessed at.
    """
    queue = OperationQueue()
    conflicts: list[DiffEntry] = []
    for d in diffs:
        if d.status == DiffStatus.LEFT_ONLY:
            queue.add(_copy_op(d, OpSide.LEFT_TO_RIGHT))
        elif d.status == DiffStatus.RIGHT_ONLY:
            queue.add(_copy_op(d, OpSide.RIGHT_TO_LEFT))
        elif d.status == DiffStatus.MODIFIED:
            conflicts.append(d)
    return queue, conflicts


def _copy_op(d: DiffEntry, side: OpSide) -> QueuedOp:
    entry = d.left if side == OpSide.LEFT_TO_RIGHT else d.right
    return QueuedOp(OpKind.COPY, side, d.rel_path, _is_dir(entry), size=entry.size if entry else 0)


def _is_dir(entry) -> bool:
    return bool(entry and entry.kind == EntryKind.DIR)
