from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from typing import Callable

from .trash import send_to_trash

ProgressCallback = Callable[[int, int, str], None]  # (bytes_done, bytes_total, current_name)
CancelCheck = Callable[[], bool]
CHUNK_SIZE = 1024 * 1024  # 1 MiB


class OperationCancelled(Exception):
    pass


@dataclass
class TransferResult:
    ok: bool
    error: str | None = None
    cancelled: bool = False


def _atomic_copy_file(
    src: str, dst: str, progress: ProgressCallback | None = None, cancel_check: CancelCheck | None = None
) -> None:
    """Copy one file into dst via a hidden temp file + atomic rename.

    The destination is never seen half-written: a reader either sees the old
    file or the fully-copied new one, never something in between. Metadata
    (mode, mtime) is preserved with copystat.
    """
    dst_dir = os.path.dirname(dst)
    os.makedirs(dst_dir, exist_ok=True)
    tmp_path = os.path.join(dst_dir, f".{os.path.basename(dst)}.{os.getpid()}.tmp")

    total = os.path.getsize(src)
    done = 0
    name = os.path.basename(src)
    if progress:
        progress(0, total, name)
    try:
        with open(src, "rb") as fsrc, open(tmp_path, "wb") as fdst:
            while True:
                # Checked per chunk so a cancel stops mid-file; the except
                # below removes the temp file, dst is never touched.
                if cancel_check and cancel_check():
                    raise OperationCancelled()
                chunk = fsrc.read(CHUNK_SIZE)
                if not chunk:
                    break
                fdst.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total, name)
        shutil.copystat(src, tmp_path, follow_symlinks=False)
        os.replace(tmp_path, dst)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def copy_item(
    src: str, dst: str, progress: ProgressCallback | None = None, cancel_check: CancelCheck | None = None
) -> TransferResult:
    """Copy a file or directory tree atomically, file by file."""
    try:
        if cancel_check and cancel_check():
            raise OperationCancelled()
        if os.path.islink(src):
            link_target = os.readlink(src)
            dst_dir = os.path.dirname(dst)
            os.makedirs(dst_dir, exist_ok=True)
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(link_target, dst)
        elif os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)
            for entry in os.scandir(src):
                sub_result = copy_item(entry.path, os.path.join(dst, entry.name), progress, cancel_check)
                if not sub_result.ok:
                    return sub_result
            shutil.copystat(src, dst, follow_symlinks=False)
        else:
            _atomic_copy_file(src, dst, progress, cancel_check)
        return TransferResult(ok=True)
    except OperationCancelled:
        return TransferResult(ok=False, error="cancelled", cancelled=True)
    except Exception as e:
        return TransferResult(ok=False, error=str(e))


def move_item(
    src: str, dst: str, progress: ProgressCallback | None = None, cancel_check: CancelCheck | None = None
) -> TransferResult:
    """Move a file or directory. Same-filesystem moves are atomic (os.replace);
    cross-filesystem moves fall back to copy + trash-the-source, so a failed
    or interrupted move never leaves the source half-deleted.
    """
    try:
        dst_dir = os.path.dirname(dst)
        os.makedirs(dst_dir, exist_ok=True)
        try:
            os.rename(src, dst)
            return TransferResult(ok=True)
        except OSError:
            pass  # cross-device or dst exists as a different type; fall through

        # A cancelled or failed copy returns here, before the source is
        # trashed -- the source is only removed after a complete copy.
        result = copy_item(src, dst, progress, cancel_check)
        if not result.ok:
            return result
        if not send_to_trash(src):
            return TransferResult(ok=False, error="Copied to destination but could not remove source")
        return TransferResult(ok=True)
    except Exception as e:
        return TransferResult(ok=False, error=str(e))


def delete_item(path: str) -> TransferResult:
    if send_to_trash(path):
        return TransferResult(ok=True)
    return TransferResult(ok=False, error="All trash methods failed")


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify_copy(src: str, dst: str) -> bool:
    """Checksum verification for critical transfers (USB/network)."""
    if os.path.isdir(src):
        for entry in os.scandir(src):
            if not verify_copy(entry.path, os.path.join(dst, entry.name)):
                return False
        return True
    return os.path.exists(dst) and sha256_of(src) == sha256_of(dst)
