from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from .models import OpKind, OpSide, OperationQueue
from .scanner import tree_stats
from .sync import copy_item, delete_item, move_item

OpProgressCallback = Callable[[int, int, str], None]  # (op_index, op_total, description)


@dataclass
class TransferStatus:
    file_name: str = ""
    file_done: int = 0
    file_total: int = 0
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    ops_done: int = 0
    ops_total: int = 0


TransferCallback = Callable[[TransferStatus], None]


@dataclass
class ExecutionReport:
    succeeded: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (rel_path, error)
    cancelled: bool = False


def execute(
    queue: OperationQueue,
    left_root: str,
    right_root: str,
    on_progress: OpProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    on_transfer: TransferCallback | None = None,
) -> ExecutionReport:
    report = ExecutionReport()
    total = len(queue)
    status = TransferStatus(ops_total=total)

    # Per-op (files, bytes), so the overall bar can snap to the exact
    # position after each op -- renames and deletes move no bytes but
    # still have to advance it.
    op_stats: list[tuple[int, int]] = []
    if on_transfer:
        for op in queue.ops:
            if op.kind == OpKind.DELETE:
                op_stats.append((0, 0))
                continue
            src_root, _ = _roots_for(op.side, left_root, right_root)
            files, _dirs, size = tree_stats([os.path.join(src_root, op.rel_path)], cancel_check)
            op_stats.append((files, size))
        status.files_total = sum(f for f, _ in op_stats)
        status.bytes_total = sum(b for _, b in op_stats)
        on_transfer(status)

    base_files = base_bytes = 0
    current = {"done": 0}

    def file_progress(done: int, file_total: int, name: str) -> None:
        if done == 0:  # a new file starts
            status.files_done += 1
            current["done"] = 0
        status.bytes_done += done - current["done"]
        current["done"] = done
        status.file_name, status.file_done, status.file_total = name, done, file_total
        on_transfer(status)

    byte_progress = file_progress if on_transfer else None

    for i, op in enumerate(queue.ops):
        if cancel_check and cancel_check():
            report.cancelled = True
            break
        if on_progress:
            on_progress(i, total, op.rel_path)

        try:
            if op.kind == OpKind.COPY:
                src_root, dst_root = _roots_for(op.side, left_root, right_root)
                result = copy_item(
                    os.path.join(src_root, op.rel_path),
                    os.path.join(dst_root, op.rel_path),
                    byte_progress,
                    cancel_check,
                )
            elif op.kind == OpKind.MOVE:
                src_root, dst_root = _roots_for(op.side, left_root, right_root)
                result = move_item(
                    os.path.join(src_root, op.rel_path),
                    os.path.join(dst_root, op.rel_path),
                    byte_progress,
                    cancel_check,
                )
            else:  # DELETE
                root = left_root if op.side == OpSide.LEFT else right_root
                result = delete_item(os.path.join(root, op.rel_path))

            if result.cancelled:
                report.cancelled = True
                break
            if result.ok:
                report.succeeded += 1
            else:
                report.failed.append((op.rel_path, result.error or "unknown error"))
        except Exception as e:
            report.failed.append((op.rel_path, str(e)))

        if on_transfer:
            base_files += op_stats[i][0]
            base_bytes += op_stats[i][1]
            status.files_done, status.bytes_done = base_files, base_bytes
            status.ops_done = i + 1
            on_transfer(status)

    if on_progress:
        on_progress(total, total, "done")
    return report


def _roots_for(side: OpSide, left_root: str, right_root: str) -> tuple[str, str]:
    if side == OpSide.LEFT_TO_RIGHT:
        return left_root, right_root
    if side == OpSide.RIGHT_TO_LEFT:
        return right_root, left_root
    raise ValueError(f"_roots_for is only valid for cross-pane ops, got {side}")
