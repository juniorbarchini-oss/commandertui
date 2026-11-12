"""Plan conflict decisions, then execute atomic copies and verified moves.

MERGE is selected by a top-level directory override. The default is always a
per-file resolution. Reports count copied leaf files (including symlinks), and
list skipped source paths. identical_kept_paths contains absolute source paths
kept by an explicit skip policy; identical_kept counts those paths. The execution
report counts only reached items, including when cancelled.
on_item(item, result) runs after each planned item.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from . import rclone_bridge, sync
from .conflicts import ConflictReport, Resolution, allowed_resolutions, rename_target, resolve
from .models import EntryKind, OpKind
from .scanner import scan_directory


@dataclass
class ConflictPolicy:
    default: Resolution
    overrides: dict[str, Resolution] = field(default_factory=dict)


@dataclass(frozen=True)
class TransferItem:
    src_abs: str
    dst_abs: str
    action: Literal["copy", "skip", "rename"]
    replace_existing: bool = False
    is_dir_whole: bool = False


@dataclass
class TransferPlan:
    items: list[TransferItem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    source_dirs: list[str] = field(default_factory=list)
    identical_kept: int = 0
    identical_kept_paths: list[str] = field(default_factory=list)


@dataclass
class TransferReport:
    copied: int = 0
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False
    identical_kept: int = 0
    identical_kept_paths: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.cancelled


def build_transfer_plan(
    sources, dst_dir, kind: OpKind, report: ConflictReport, policy: ConflictPolicy
) -> TransferPlan:
    allowed = allowed_resolutions(kind)
    if policy.default in (Resolution.MERGE, Resolution.CANCEL):
        raise ValueError("default must be a per-file resolution, not MERGE or CANCEL")
    if any(r not in allowed or r == Resolution.CANCEL for r in [policy.default, *policy.overrides.values()]):
        raise ValueError(f"Invalid resolution for {kind}")
    conflicts = defaultdict(list)
    for conflict in report.conflicts:
        conflicts[conflict.rel_path].append(conflict)
    identical_paths = set(report.identical_paths)
    source_names = [os.path.basename(os.path.abspath(source)) for source in sources]
    seen_names: set[str] = set()
    plan = TransferPlan()
    reserved: set[str] = set()

    def renamed(dst):
        candidate = rename_target(dst)
        if candidate in reserved:
            stem, ext = os.path.splitext(dst)
            index = 1
            while os.path.lexists(f"{stem}_{index}{ext}") or f"{stem}_{index}{ext}" in reserved:
                index += 1
            candidate = f"{stem}_{index}{ext}"
        reserved.add(candidate)
        return candidate

    def add(src, dst, rel, whole=False):
        conflict = report.conflicts_by_source.get(src)
        candidates = conflicts.get(rel, [])
        if (
            conflict is None
            and not report.conflicts_by_source
            and src not in identical_paths
            and len(candidates) == 1
        ):
            conflict = candidates[0]
        decision = policy.overrides.get(rel, policy.default)
        if decision == Resolution.MERGE:
            raise ValueError("MERGE requires a colliding directory")
        exists = os.path.lexists(dst)
        # An unreported existing target can only be a race/stale report.
        # Refuse to overwrite it without an explicit conflict decision.
        action = resolve(conflict, decision) if conflict else "skip" if exists else "copy"
        if dst in reserved and action != "skip":
            if decision == Resolution.RENAME:
                action = "rename"
            elif decision == Resolution.SKIP:
                action = "skip"
            else:
                # Last writer wins in the plan, before any source is touched.
                for index, previous in enumerate(plan.items):
                    if previous.dst_abs == dst and previous.action != "skip":
                        plan.items[index] = TransferItem(
                            previous.src_abs, dst, "skip", False, previous.is_dir_whole
                        )
                        plan.skipped.append(previous.src_abs)
                reserved.discard(dst)
                action = "copy"
        if action == "rename":
            dst = renamed(dst)
        if action == "skip":
            plan.skipped.append(src)
        else:
            reserved.add(dst)
        plan.items.append(TransferItem(src, dst, action, exists and action == "copy", whole))

    for source_index, source in enumerate(sources):
        src = os.path.abspath(source)
        name = os.path.basename(src)
        dst = os.path.abspath(os.path.join(dst_dir, name))
        if src == dst or (
            os.path.isdir(src)
            and not os.path.islink(src)
            and os.path.commonpath([os.path.realpath(src), os.path.realpath(dst)]) == os.path.realpath(src)
        ):
            raise ValueError("Destination must be outside the source")
        decision = policy.overrides.get(name, policy.default)
        repeated = name in seen_names
        seen_names.add(name)
        later = name in source_names[source_index + 1 :]
        if (decision == Resolution.OVERWRITE and later) or (decision == Resolution.SKIP and repeated):
            plan.items.append(
                TransferItem(src, dst, "skip", False, os.path.isdir(src) and not os.path.islink(src))
            )
            plan.skipped.append(src)
            continue
        whole = os.path.isdir(src) and not os.path.islink(src)
        collision = whole and name in report.dir_collisions
        decision = policy.overrides.get(name, policy.default)
        if decision == Resolution.RENAME and (collision or repeated):
            plan.items.append(TransferItem(src, renamed(dst), "rename", False, whole))
            continue
        descendants = any(key.startswith(name + "/") for key in policy.overrides)
        if collision and decision == Resolution.SKIP and not descendants:
            plan.items.append(TransferItem(src, dst, "skip", False, True))
            plan.skipped.append(src)
            continue
        if not collision:
            add(src, dst, name, whole)
            continue
        # Both directories exist: preserve destination-only entries, and apply
        # the per-file policy. Empty directories are explicit items too.
        plan.source_dirs.append(src)
        blocked = []
        for path, entry in scan_directory(src).items():
            if any(path.startswith(parent + "/") for parent in blocked):
                continue
            rel = f"{name}/{path}"
            target = os.path.join(dst, path)
            if entry.kind == EntryKind.DIR and os.path.join(src, path) not in report.conflicts_by_source:
                plan.source_dirs.append(os.path.join(src, path))
                if not os.path.lexists(target):
                    add(os.path.join(src, path), target, rel, True)
                    blocked.append(path)
                continue
            add(os.path.join(src, path), target, rel, entry.kind == EntryKind.DIR)
            if entry.kind == EntryKind.DIR:
                blocked.append(path)
    # Include explicit whole-folder skips and duplicate-source decisions too.
    plan.identical_kept_paths = list(
        dict.fromkeys(
            path
            for path in report.identical_paths
            if any(
                item.action == "skip"
                and (item.src_abs == path or item.is_dir_whole and path.startswith(item.src_abs + os.sep))
                for item in plan.items
            )
        )
    )
    plan.identical_kept = len(plan.identical_kept_paths)
    return plan


def execute_transfer_plan(
    plan: TransferPlan, kind: OpKind, progress=None, cancel_check=None, on_item=None
) -> TransferReport:
    """Copy leaves atomically; MOVE trashes each source only after verification.

    No rename fast path is used, so cancellation and verification have the same
    behavior across filesystems. Existing same-kind trees are merged; only type
    mismatches require trashing the destination. Empty source dirs use rmdir.
    """
    if kind not in (OpKind.COPY, OpKind.MOVE):
        raise ValueError(f"Unsupported operation: {kind}")
    report = TransferReport()
    cleanup: set[str] = set()
    mounts = rclone_bridge.detect_rclone_mounts() if rclone_bridge.rclone_available() else []

    def cancelled():
        if cancel_check and cancel_check():
            report.cancelled = True
            return True
        return False

    def transfer(src, dst, replace_existing):
        if cancelled():
            return
        try:
            src_dir = os.path.isdir(src) and not os.path.islink(src)
            dst_exists = os.path.lexists(dst)
            dst_dir = os.path.isdir(dst) and not os.path.islink(dst)
            if os.path.abspath(src) == os.path.abspath(dst) or (
                not os.path.islink(src)
                and not os.path.islink(dst)
                and os.path.realpath(src) == os.path.realpath(dst)
            ):
                raise ValueError("Source and destination refer to the same path")
            if dst_exists and not replace_existing:
                raise FileExistsError(dst)
            if dst_exists and (src_dir != dst_dir or os.path.islink(src) or os.path.islink(dst)):
                if not sync.send_to_trash(dst):
                    raise OSError("Could not trash existing destination")
            if mounts and not os.path.islink(src) and rclone_bridge.is_rclone_transfer(src, dst, mounts):
                fn = rclone_bridge.copy_with_rclone if kind == OpKind.COPY else rclone_bridge.move_with_rclone
                rc_res = fn(src, dst, src_dir, replace_existing, progress, cancel_check, mounts)
                if rc_res.cancelled:
                    report.cancelled = True
                    return
                if not rc_res.ok:
                    raise OSError(rc_res.error or "rclone transfer failed")
                report.copied += rc_res.files_transferred
                return
            if src_dir:
                os.makedirs(dst, exist_ok=True)
                if kind == OpKind.MOVE:
                    cleanup.add(src)
                with os.scandir(src) as entries:
                    children = sorted(entries, key=lambda e: e.name)
                for entry in children:
                    transfer(entry.path, os.path.join(dst, entry.name), replace_existing)
                    if report.cancelled:
                        break
                return
            result = sync.copy_item(src, dst, progress, cancel_check)
            if result.cancelled:
                report.cancelled = True
                return
            if not result.ok:
                raise OSError(result.error)
            if kind == OpKind.MOVE:
                verified = (
                    os.path.islink(dst) and os.readlink(src) == os.readlink(dst)
                    if os.path.islink(src)
                    else sync.verify_copy(src, dst)
                )
                if not verified:
                    raise OSError("Copy verification failed; source retained")
                if not sync.send_to_trash(src):
                    raise OSError("Copied but could not trash source")
            report.copied += 1
        except Exception as error:
            report.failed.append((src, str(error)))

    def _can_batch_rclone_file(it: TransferItem) -> bool:
        if not mounts or it.action != "copy" or it.is_dir_whole:
            return False
        if os.path.basename(it.src_abs) != os.path.basename(it.dst_abs):
            return False
        if os.path.islink(it.src_abs) or os.path.isdir(it.src_abs):
            return False
        if os.path.lexists(it.dst_abs):
            if not it.replace_existing or os.path.isdir(it.dst_abs) or os.path.islink(it.dst_abs):
                return False
        return rclone_bridge.is_rclone_transfer(it.src_abs, it.dst_abs, mounts)

    idx = 0
    n_items = len(plan.items)
    while idx < n_items:
        if cancelled():
            break
        item = plan.items[idx]
        if item.action == "skip":
            report.skipped.append(item.src_abs)
            kept = [
                path
                for path in plan.identical_kept_paths
                if path == item.src_abs or item.is_dir_whole and path.startswith(item.src_abs + os.sep)
            ]
            report.identical_kept_paths.extend(
                path for path in kept if path not in report.identical_kept_paths
            )
            report.identical_kept = len(report.identical_kept_paths)
            if on_item:
                on_item(item, report)
            idx += 1
            continue

        if _can_batch_rclone_file(item):
            batch = [item]
            j = idx + 1
            while j < n_items and _can_batch_rclone_file(plan.items[j]):
                batch.append(plan.items[j])
                j += 1
            if len(batch) >= 2:
                src_root = os.path.commonpath([os.path.dirname(it.src_abs) for it in batch])
                dst_root = os.path.commonpath([os.path.dirname(it.dst_abs) for it in batch])
                rels = [os.path.relpath(it.src_abs, src_root) for it in batch]
                if all(rel == os.path.relpath(it.dst_abs, dst_root) for rel, it in zip(rels, batch)):
                    rc_res = rclone_bridge.batch_transfer_files_with_rclone(
                        rels,
                        src_root,
                        dst_root,
                        kind,
                        replace_existing=any(it.replace_existing for it in batch),
                        progress=progress,
                        cancel_check=cancel_check,
                        mounts=mounts,
                    )
                    if rc_res.cancelled:
                        report.cancelled = True
                        break
                    if not rc_res.ok:
                        report.failed.append((batch[0].src_abs, rc_res.error or "rclone batch failed"))
                    else:
                        report.copied += len(batch)
                    for it in batch:
                        if on_item:
                            on_item(it, report)
                    idx = j
                    continue

        transfer(item.src_abs, item.dst_abs, item.replace_existing)
        if on_item:
            on_item(item, report)
        if report.cancelled:
            break
        idx += 1

    if kind == OpKind.MOVE and report.ok:
        cleanup.update(plan.source_dirs)
        for directory in sorted(cleanup, key=lambda p: p.count(os.sep), reverse=True):
            try:
                os.rmdir(directory)  # succeeds only when empty; never recursive
            except OSError:
                pass
    return report
