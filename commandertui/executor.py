from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from . import rclone_bridge
from .models import OpKind, OpSide, OperationQueue, QueuedOp
from .scanner import tree_stats
from .sync import TransferResult, copy_item, delete_item, move_item

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
    mounts = rclone_bridge.detect_rclone_mounts() if rclone_bridge.rclone_available() else []

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

    def _can_batch_op(op: QueuedOp) -> bool:
        if not mounts or op.kind not in (OpKind.COPY, OpKind.MOVE) or op.is_dir:
            return False
        src_root, dst_root = _roots_for(op.side, left_root, right_root)
        src_path = os.path.join(src_root, op.rel_path)
        if os.path.islink(src_path) or os.path.isdir(src_path):
            return False
        return rclone_bridge.is_rclone_transfer(src_root, dst_root, mounts)

    i = 0
    while i < total:
        if cancel_check and cancel_check():
            report.cancelled = True
            break
        op = queue.ops[i]
        if on_progress:
            on_progress(i, total, op.rel_path)

        if _can_batch_op(op):
            batch_indices = [i]
            j = i + 1
            while j < total:
                next_op = queue.ops[j]
                if next_op.kind == op.kind and next_op.side == op.side and _can_batch_op(next_op):
                    batch_indices.append(j)
                    j += 1
                else:
                    break
            if len(batch_indices) >= 2:
                src_root, dst_root = _roots_for(op.side, left_root, right_root)
                rels = [queue.ops[k].rel_path for k in batch_indices]
                rc_res = rclone_bridge.batch_transfer_files_with_rclone(
                    rels,
                    src_root,
                    dst_root,
                    op.kind,
                    replace_existing=True,
                    progress=byte_progress,
                    cancel_check=cancel_check,
                    mounts=mounts,
                )
                if rc_res.cancelled:
                    report.cancelled = True
                    break
                if rc_res.ok:
                    report.succeeded += len(batch_indices)
                elif rc_res.is_config_error:
                    # Config error fallback to normal motor for batch
                    for k in batch_indices:
                        b_op = queue.ops[k]
                        b_fn = copy_item if b_op.kind == OpKind.COPY else move_item
                        b_src = os.path.join(src_root, b_op.rel_path)
                        b_dst = os.path.join(dst_root, b_op.rel_path)
                        b_res = b_fn(b_src, b_dst, byte_progress, cancel_check)
                        if b_res.cancelled:
                            report.cancelled = True
                            break
                        if b_res.ok:
                            report.succeeded += 1
                        else:
                            report.failed.append((b_op.rel_path, b_res.error or "transfer failed"))
                    if report.cancelled:
                        break
                else:
                    succeeded_set = {os.path.normpath(name) for name in rc_res.succeeded}
                    failed_map = {os.path.normpath(name): err for name, err in rc_res.failed}
                    for k in batch_indices:
                        rel = queue.ops[k].rel_path
                        norm = os.path.normpath(rel)
                        if norm in failed_map:
                            report.failed.append((rel, failed_map[norm]))
                        elif norm in succeeded_set:
                            report.succeeded += 1
                        else:
                            report.failed.append((rel, rc_res.error or "rclone batch failed"))
                if on_transfer:
                    for k in batch_indices:
                        base_files += op_stats[k][0]
                        base_bytes += op_stats[k][1]
                    status.files_done, status.bytes_done = base_files, base_bytes
                    status.ops_done = batch_indices[-1] + 1
                    on_transfer(status)
                i = j
                continue

        try:
            if op.kind in (OpKind.COPY, OpKind.MOVE):
                src_root, dst_root = _roots_for(op.side, left_root, right_root)
                src_path = os.path.join(src_root, op.rel_path)
                dst_path = os.path.join(dst_root, op.rel_path)
                is_dir = os.path.isdir(src_path)
                has_symlinks = is_dir and rclone_bridge.tree_has_symlinks(src_path)
                if (
                    mounts
                    and not os.path.islink(src_path)
                    and not has_symlinks
                    and rclone_bridge.is_rclone_transfer(src_path, dst_path, mounts)
                ):
                    fn = (
                        rclone_bridge.copy_with_rclone
                        if op.kind == OpKind.COPY
                        else rclone_bridge.move_with_rclone
                    )
                    rc_res = fn(
                        src_path,
                        dst_path,
                        is_dir,
                        True,
                        byte_progress,
                        cancel_check,
                        mounts,
                    )
                    if not rc_res.ok and rc_res.is_config_error:
                        fn_normal = copy_item if op.kind == OpKind.COPY else move_item
                        result = fn_normal(src_path, dst_path, byte_progress, cancel_check)
                    else:
                        result = TransferResult(ok=rc_res.ok, error=rc_res.error, cancelled=rc_res.cancelled)
                elif op.kind == OpKind.COPY:
                    result = copy_item(src_path, dst_path, byte_progress, cancel_check)
                else:
                    result = move_item(src_path, dst_path, byte_progress, cancel_check)
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
        i += 1

    if on_progress:
        on_progress(total, total, "done")
    return report


def _roots_for(side: OpSide, left_root: str, right_root: str) -> tuple[str, str]:
    if side == OpSide.LEFT_TO_RIGHT:
        return left_root, right_root
    if side == OpSide.RIGHT_TO_LEFT:
        return right_root, left_root
    raise ValueError(f"_roots_for is only valid for cross-pane ops, got {side}")
