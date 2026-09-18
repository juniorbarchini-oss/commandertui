from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from .models import OpKind, OpSide, OperationQueue
from .sync import copy_item, delete_item, move_item

OpProgressCallback = Callable[[int, int, str], None]  # (op_index, op_total, description)


@dataclass
class ExecutionReport:
    succeeded: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (rel_path, error)


def execute(
    queue: OperationQueue,
    left_root: str,
    right_root: str,
    on_progress: OpProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ExecutionReport:
    report = ExecutionReport()
    total = len(queue)

    for i, op in enumerate(queue.ops):
        if cancel_check and cancel_check():
            break
        if on_progress:
            on_progress(i, total, op.rel_path)

        try:
            if op.kind == OpKind.COPY:
                src_root, dst_root = _roots_for(op.side, left_root, right_root)
                result = copy_item(
                    os.path.join(src_root, op.rel_path), os.path.join(dst_root, op.rel_path)
                )
            elif op.kind == OpKind.MOVE:
                src_root, dst_root = _roots_for(op.side, left_root, right_root)
                result = move_item(
                    os.path.join(src_root, op.rel_path), os.path.join(dst_root, op.rel_path)
                )
            else:  # DELETE
                root = left_root if op.side == OpSide.LEFT else right_root
                result = delete_item(os.path.join(root, op.rel_path))

            if result.ok:
                report.succeeded += 1
            else:
                report.failed.append((op.rel_path, result.error or "unknown error"))
        except Exception as e:
            report.failed.append((op.rel_path, str(e)))

    if on_progress:
        on_progress(total, total, "done")
    return report


def _roots_for(side: OpSide, left_root: str, right_root: str) -> tuple[str, str]:
    if side == OpSide.LEFT_TO_RIGHT:
        return left_root, right_root
    if side == OpSide.RIGHT_TO_LEFT:
        return right_root, left_root
    raise ValueError(f"_roots_for is only valid for cross-pane ops, got {side}")
