"""Create and extract `.tar.gz` (and `.tar`, `.tgz`, `.tar.bz2`, `.tar.xz`, `.zip`) archives.

Features:
- Streamed creation of `.tar.gz` archives with per-file/chunk byte progress and
  `Esc` cancellation (partial temp archive is cleaned up automatically).
- Safe extraction with strict path-traversal validation (`..` or absolute member
  paths are rejected before touching disk).
- Cloud-mount aware (`fuse.rclone`): when the archive lives on an `rclone`
  mount (e.g. `/mnt/gdrive`), creating an archive builds it in a local temp
  file and uploads it via `rclone_bridge.copy_with_rclone`; extracting downloads
  a temp copy first if needed and flushes the mount cache when writing into a
  mounted destination.
"""

from __future__ import annotations

import os
import tempfile
import time
import tarfile
import zipfile
from dataclasses import dataclass, field, replace
from typing import Callable

from .executor import TransferStatus
from .modals import ChoiceScreen, InputScreen, MessageScreen
from .progress_screen import ProgressScreen
from .rclone_bridge import (
    copy_with_rclone,
    detect_rclone_mounts,
    flush_mount_cache,
    rclone_available,
    resolve_rclone_endpoint,
)
from .scanner import tree_stats
from .trash import send_to_trash

ProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]

CHUNK_SIZE = 256 * 1024

ARCHIVE_EXTENSIONS = (
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".tar",
    ".zip",
)


@dataclass
class ArchiveResult:
    ok: bool
    archive_path: str = ""
    files_processed: int = 0
    bytes_processed: int = 0
    cancelled: bool = False
    error: str | None = None
    failed: list[tuple[str, str]] = field(default_factory=list)


def is_supported_archive(path: str) -> bool:
    lower = os.path.basename(path).lower()
    return any(lower.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def strip_archive_extension(filename: str) -> str:
    lower = filename.lower()
    for ext in ARCHIVE_EXTENSIONS:
        if lower.endswith(ext) and len(filename) > len(ext):
            return filename[: -len(ext)]
    base, _ = os.path.splitext(filename)
    return base or filename


def default_archive_name(sources: list[str], src_dir: str) -> str:
    if len(sources) == 1:
        base = os.path.basename(sources[0].rstrip(os.sep))
    else:
        base = os.path.basename(src_dir.rstrip(os.sep)) or "archive"
    if not base.lower().endswith((".tar.gz", ".tgz")):
        base = f"{base}.tar.gz"
    return base


class _ProgressFileReader:
    """File-like wrapper that emits byte progress and checks for cancellation."""

    def __init__(
        self,
        fp,
        total_size: int,
        label: str,
        progress: ProgressCallback | None,
        cancel_check: CancelCheck | None,
    ) -> None:
        self._fp = fp
        self._total = total_size
        self._label = label
        self._progress = progress
        self._cancel_check = cancel_check
        self._done = 0

    def read(self, size: int = -1) -> bytes:
        if self._cancel_check and self._cancel_check():
            raise InterruptedError("cancelled")
        chunk = self._fp.read(size if size > 0 else CHUNK_SIZE)
        if chunk:
            self._done += len(chunk)
            if self._progress:
                self._progress(self._done, max(self._total, self._done, 1), self._label)
        return chunk

    def __getattr__(self, name: str):
        return getattr(self._fp, name)


def _iter_source_entries(sources: list[str], cancel_check: CancelCheck | None = None):
    """Yield `(abs_path, arcname, is_dir, is_symlink)` for all items in `sources`."""
    for src in sources:
        if cancel_check and cancel_check():
            return
        src_abs = os.path.abspath(src)
        top_name = os.path.basename(src_abs.rstrip(os.sep))
        if os.path.islink(src_abs) or not os.path.isdir(src_abs):
            yield src_abs, top_name, False, os.path.islink(src_abs)
            continue

        yield src_abs, top_name, True, False
        for root, dirs, files in os.walk(src_abs, followlinks=False):
            if cancel_check and cancel_check():
                return
            dirs.sort()
            files.sort()
            rel_root = os.path.relpath(root, src_abs)
            arc_prefix = top_name if rel_root == "." else os.path.join(top_name, rel_root)
            for d in dirs:
                d_abs = os.path.join(root, d)
                d_arc = os.path.join(arc_prefix, d).replace(os.sep, "/")
                yield d_abs, d_arc, not os.path.islink(d_abs), os.path.islink(d_abs)
            for f in files:
                f_abs = os.path.join(root, f)
                f_arc = os.path.join(arc_prefix, f).replace(os.sep, "/")
                yield f_abs, f_arc, False, os.path.islink(f_abs)


def create_tar_gz(
    sources: list[str],
    output_path: str,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    compresslevel: int = 6,
) -> ArchiveResult:
    """Create a `.tar.gz` archive from `sources` into `output_path`.

    Writes to a temporary file first so a cancelled or failed run never leaves
    a corrupt archive at `output_path`. If `output_path` is on an `rclone`
    mount, builds the archive in local temp storage and uploads it via `rclone`.
    """
    if not sources:
        return ArchiveResult(ok=False, error="No sources selected")

    out_abs = os.path.abspath(output_path)
    out_dir = os.path.dirname(out_abs)
    os.makedirs(out_dir, exist_ok=True)

    mounts = detect_rclone_mounts() if rclone_available() else []
    _, dst_is_rclone = resolve_rclone_endpoint(out_abs, mounts) if mounts else (out_abs, False)

    temp_dir = tempfile.gettempdir() if dst_is_rclone else out_dir
    fd, tmp_archive = tempfile.mkstemp(prefix=".commandertui-tar-", suffix=".tar.gz", dir=temp_dir)
    os.close(fd)

    files_done = 0
    bytes_done = 0

    try:
        with tarfile.open(tmp_archive, mode="w:gz", compresslevel=compresslevel) as tf:
            for abs_path, arcname, is_dir, is_symlink in _iter_source_entries(sources, cancel_check):
                if cancel_check and cancel_check():
                    raise InterruptedError("cancelled")

                # Skip the temporary archive itself if output is inside one of the source dirs
                if os.path.abspath(abs_path) in (out_abs, os.path.abspath(tmp_archive)):
                    continue

                if is_dir or is_symlink:
                    tf.add(abs_path, arcname=arcname, recursive=False)
                    continue

                st = os.lstat(abs_path)
                info = tf.gettarinfo(abs_path, arcname=arcname)
                label = os.path.basename(abs_path)
                if progress:
                    progress(0, max(st.st_size, 1), label)

                with open(abs_path, "rb") as raw_fp:
                    wrapped = _ProgressFileReader(raw_fp, st.st_size, label, progress, cancel_check)
                    tf.addfile(info, fileobj=wrapped)

                files_done += 1
                bytes_done += st.st_size

        if cancel_check and cancel_check():
            raise InterruptedError("cancelled")

        if dst_is_rclone:
            rc_res = copy_with_rclone(
                tmp_archive,
                out_abs,
                is_dir=False,
                replace_existing=True,
                progress=progress,
                cancel_check=cancel_check,
                mounts=mounts,
            )
            if rc_res.cancelled:
                raise InterruptedError("cancelled")
            if not rc_res.ok:
                return ArchiveResult(
                    ok=False,
                    archive_path=out_abs,
                    files_processed=files_done,
                    bytes_processed=bytes_done,
                    error=rc_res.error or "Failed to upload archive to cloud mount",
                )
        else:
            os.replace(tmp_archive, out_abs)

        return ArchiveResult(
            ok=True,
            archive_path=out_abs,
            files_processed=files_done,
            bytes_processed=bytes_done,
        )
    except InterruptedError:
        return ArchiveResult(
            ok=False,
            archive_path=out_abs,
            files_processed=files_done,
            bytes_processed=bytes_done,
            cancelled=True,
        )
    except Exception as exc:
        return ArchiveResult(
            ok=False,
            archive_path=out_abs,
            files_processed=files_done,
            bytes_processed=bytes_done,
            error=str(exc),
        )
    finally:
        if os.path.lexists(tmp_archive):
            try:
                os.remove(tmp_archive)
            except OSError:
                pass


def _is_within_directory(base_dir: str, target_path: str) -> bool:
    base_real = os.path.realpath(base_dir)
    target_real = os.path.realpath(target_path)
    return target_real == base_real or target_real.startswith(base_real + os.sep)


def _validate_tar_member(member: tarfile.TarInfo, dest_dir: str) -> str:
    """Validate that a tar member does not escape `dest_dir`."""
    name = member.name.lstrip("/")
    if not name or name == ".":
        return dest_dir
    norm = os.path.normpath(name)
    if (
        norm.startswith("..")
        or norm.startswith(os.sep)
        or (os.sep + ".." + os.sep) in (os.sep + norm + os.sep)
    ):
        raise ValueError(f"Unsafe archive member path rejected: {member.name}")
    target = os.path.abspath(os.path.join(dest_dir, norm))
    base_abs = os.path.abspath(dest_dir)
    if target != base_abs and not target.startswith(base_abs + os.sep):
        raise ValueError(f"Unsafe archive member path rejected: {member.name}")
    if member.issym() or member.islnk():
        link_target = (
            os.path.join(dest_dir, member.linkname)
            if member.islnk()
            else os.path.join(os.path.dirname(target), member.linkname)
        )
        link_abs = os.path.abspath(os.path.normpath(link_target))
        if link_abs != base_abs and not link_abs.startswith(base_abs + os.sep):
            raise ValueError(f"Unsafe symlink/hardlink target rejected: {member.name} -> {member.linkname}")
    return target


def extract_archive(
    archive_path: str,
    dest_dir: str,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> ArchiveResult:
    """Safely extract `.tar.gz`/`.tgz`/`.tar.*`/`.zip` into `dest_dir`."""
    arc_abs = os.path.abspath(archive_path)
    dst_abs = os.path.abspath(dest_dir)
    if not os.path.isfile(arc_abs):
        return ArchiveResult(ok=False, archive_path=arc_abs, error="Archive file not found")

    os.makedirs(dst_abs, exist_ok=True)
    mounts = detect_rclone_mounts() if rclone_available() else []
    _, dst_is_rclone = resolve_rclone_endpoint(dst_abs, mounts) if mounts else (dst_abs, False)

    files_done = 0
    bytes_done = 0

    try:
        if arc_abs.lower().endswith(".zip") and zipfile.is_zipfile(arc_abs):
            with zipfile.ZipFile(arc_abs, "r") as zf:
                infos = zf.infolist()
                for info in infos:
                    if cancel_check and cancel_check():
                        raise InterruptedError("cancelled")
                    norm = os.path.normpath(info.filename)
                    if norm.startswith("..") or os.path.isabs(info.filename):
                        raise ValueError(f"Unsafe zip member path rejected: {info.filename}")
                    target = os.path.abspath(os.path.join(dst_abs, norm))
                    if target != dst_abs and not target.startswith(dst_abs + os.sep):
                        raise ValueError(f"Unsafe zip member path rejected: {info.filename}")
                for info in infos:
                    if cancel_check and cancel_check():
                        raise InterruptedError("cancelled")
                    norm = os.path.normpath(info.filename)
                    target = os.path.abspath(os.path.join(dst_abs, norm))
                    if info.is_dir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    label = os.path.basename(target)
                    total = max(info.file_size, 1)
                    if progress:
                        progress(0, total, label)
                    done_file = 0
                    with zf.open(info, "r") as src_fp, open(target, "wb") as dst_fp:
                        while True:
                            if cancel_check and cancel_check():
                                raise InterruptedError("cancelled")
                            chunk = src_fp.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            dst_fp.write(chunk)
                            done_file += len(chunk)
                            if progress:
                                progress(done_file, max(total, done_file), label)
                    files_done += 1
                    bytes_done += done_file
        else:
            with tarfile.open(arc_abs, mode="r:*") as tf:
                members = tf.getmembers()
                # Pre-validate all members before writing anything
                for member in members:
                    if cancel_check and cancel_check():
                        raise InterruptedError("cancelled")
                    _validate_tar_member(member, dst_abs)

                for member in members:
                    if cancel_check and cancel_check():
                        raise InterruptedError("cancelled")
                    target = _validate_tar_member(member, dst_abs)
                    if target == dst_abs:
                        continue
                    if member.isdir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    if member.isfile():
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        label = os.path.basename(target)
                        total = max(member.size, 1)
                        if progress:
                            progress(0, total, label)
                        done_file = 0
                        src_fp = tf.extractfile(member)
                        if src_fp is not None:
                            with src_fp, open(target, "wb") as dst_fp:
                                while True:
                                    if cancel_check and cancel_check():
                                        raise InterruptedError("cancelled")
                                    chunk = src_fp.read(CHUNK_SIZE)
                                    if not chunk:
                                        break
                                    dst_fp.write(chunk)
                                    done_file += len(chunk)
                                    if progress:
                                        progress(done_file, max(total, done_file), label)
                        try:
                            if member.mode:
                                os.chmod(target, member.mode & 0o777)
                            if member.mtime:
                                os.utime(target, (member.mtime, member.mtime))
                        except OSError:
                            pass
                        files_done += 1
                        bytes_done += done_file
                    elif member.issym() or member.islnk():
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        if os.path.lexists(target):
                            os.remove(target)
                        if member.issym():
                            os.symlink(member.linkname, target)
                        else:
                            os.link(os.path.join(dst_abs, member.linkname), target)

        if dst_is_rclone:
            flush_mount_cache()

        return ArchiveResult(
            ok=True,
            archive_path=arc_abs,
            files_processed=files_done,
            bytes_processed=bytes_done,
        )
    except InterruptedError:
        return ArchiveResult(
            ok=False,
            archive_path=arc_abs,
            files_processed=files_done,
            bytes_processed=bytes_done,
            cancelled=True,
        )
    except Exception as exc:
        return ArchiveResult(
            ok=False,
            archive_path=arc_abs,
            files_processed=files_done,
            bytes_processed=bytes_done,
            error=str(exc),
        )


def archive_uncompressed_stats(archive_path: str, cancel_check: CancelCheck | None = None) -> tuple[int, int]:
    """Return `(file_count, total_uncompressed_bytes)` for an archive."""
    arc_abs = os.path.abspath(archive_path)
    try:
        if arc_abs.lower().endswith(".zip") and zipfile.is_zipfile(arc_abs):
            with zipfile.ZipFile(arc_abs, "r") as zf:
                files = [i for i in zf.infolist() if not i.is_dir()]
                return len(files), sum(i.file_size for i in files)
        with tarfile.open(arc_abs, mode="r:*") as tf:
            count = 0
            total = 0
            for m in tf:
                if cancel_check and cancel_check():
                    break
                if m.isfile():
                    count += 1
                    total += m.size
            return count, total
    except Exception:
        try:
            return 1, os.path.getsize(arc_abs)
        except OSError:
            return 1, 0


def run_create_archive(owner, src_panel, dst_panel, sources: list[str]) -> None:
    """Prompt for archive destination (defaulting to the opposite panel) and build `.tar.gz`."""
    default_name = default_archive_name(sources, src_panel.path)
    default_target = os.path.join(dst_panel.path, default_name)

    def on_input(target: str | None) -> None:
        if not target or not target.strip():
            return
        raw = target.strip()
        if not raw.lower().endswith((".tar.gz", ".tgz")):
            raw = f"{raw}.tar.gz"
        out_path = raw if os.path.isabs(raw) else os.path.abspath(os.path.join(dst_panel.path, raw))

        if os.path.lexists(out_path):
            owner.app.push_screen(
                ChoiceScreen(
                    f"{os.path.basename(out_path)} already exists in destination:", ["Overwrite", "Cancel"]
                ),
                lambda choice: _start_pack(out_path, trash_existing=True) if choice == "Overwrite" else None,
            )
        else:
            _start_pack(out_path, trash_existing=False)

    def _start_pack(out_path: str, trash_existing: bool) -> None:
        progress = ProgressScreen(f"Creating {os.path.basename(out_path)}", len(sources))
        owner.app.push_screen(progress)

        def work() -> None:
            if trash_existing and os.path.lexists(out_path):
                mounts = detect_rclone_mounts() if rclone_available() else []
                _, is_rc = resolve_rclone_endpoint(out_path, mounts) if mounts else (out_path, False)
                if not is_rc and not send_to_trash(out_path):
                    owner.app.call_from_thread(
                        finish,
                        ArchiveResult(
                            ok=False, archive_path=out_path, error="Could not move existing archive to trash"
                        ),
                    )
                    return

            files, _, total_bytes = tree_stats(sources, lambda: progress.cancel_requested)
            status = TransferStatus(
                ops_total=len(sources),
                files_total=max(files, 1),
                bytes_total=total_bytes,
            )
            last = 0.0
            current_done = 0

            def update(done: int, total: int, name: str) -> None:
                nonlocal last, current_done
                if done == 0:
                    current_done = 0
                    status.files_done += 1
                status.bytes_done += max(0, done - current_done)
                current_done = done
                status.file_name, status.file_done, status.file_total = name, done, total
                now = time.monotonic()
                if now - last >= 0.1 or done == total:
                    last = now
                    owner.app.call_from_thread(progress.report, replace(status))

            res = create_tar_gz(sources, out_path, update, lambda: progress.cancel_requested)
            owner.app.call_from_thread(finish, res)

        def finish(res: ArchiveResult) -> None:
            progress.dismiss()
            if res.ok:
                src_panel.marked.clear()
            src_panel.refresh_listing()
            dst_panel.refresh_listing()
            if res.cancelled:
                owner.app.push_screen(
                    MessageScreen("Archive creation cancelled.\nThe incomplete .tar.gz file was removed.")
                )
            elif not res.ok:
                owner.app.push_screen(MessageScreen(f"Could not create archive:\n{res.error}"))
            else:
                owner.notify(f"Created {os.path.basename(res.archive_path)} ({res.files_processed} files)")

        owner.call_after_refresh(lambda: owner.run_worker(work, thread=True))

    owner.app.push_screen(
        InputScreen(f"Create .tar.gz archive ({len(sources)} selected):", initial=default_target),
        on_input,
    )


def run_extract_archive(owner, src_panel, dst_panel, archive_path: str) -> None:
    """Prompt where to extract `archive_path` (defaulting to opposite panel) and extract with progress."""
    arc_name = os.path.basename(archive_path)
    sub_name = strip_archive_extension(arc_name)
    opt_opposite_here = f"Opposite panel: {dst_panel.path}/"
    opt_opposite_sub = f"Opposite panel subfolder: {os.path.join(dst_panel.path, sub_name)}/"
    opt_current_sub = f"Current panel subfolder: {os.path.join(src_panel.path, sub_name)}/"
    opt_current_here = f"Current panel: {src_panel.path}/"

    options = [opt_opposite_here, opt_opposite_sub, opt_current_sub, opt_current_here]

    def on_choice(choice: str | None) -> None:
        if not choice:
            return
        if choice == opt_opposite_here:
            target_dir = dst_panel.path
        elif choice == opt_opposite_sub:
            target_dir = os.path.join(dst_panel.path, sub_name)
        elif choice == opt_current_sub:
            target_dir = os.path.join(src_panel.path, sub_name)
        else:
            target_dir = src_panel.path

        progress = ProgressScreen(f"Extracting {arc_name} to {target_dir}", 1)
        owner.app.push_screen(progress)

        def work() -> None:
            files_total, bytes_total = archive_uncompressed_stats(
                archive_path, lambda: progress.cancel_requested
            )
            status = TransferStatus(
                ops_total=1,
                files_total=max(files_total, 1),
                bytes_total=bytes_total,
            )
            last = 0.0
            current_done = 0

            def update(done: int, total: int, name: str) -> None:
                nonlocal last, current_done
                if done == 0:
                    current_done = 0
                    status.files_done += 1
                status.bytes_done += max(0, done - current_done)
                current_done = done
                status.file_name, status.file_done, status.file_total = name, done, total
                now = time.monotonic()
                if now - last >= 0.1 or done == total:
                    last = now
                    owner.app.call_from_thread(progress.report, replace(status))

            res = extract_archive(archive_path, target_dir, update, lambda: progress.cancel_requested)
            owner.app.call_from_thread(finish, res)

        def finish(res: ArchiveResult) -> None:
            progress.dismiss()
            src_panel.refresh_listing()
            dst_panel.refresh_listing()
            if res.cancelled:
                owner.app.push_screen(MessageScreen("Extraction cancelled."))
            elif not res.ok:
                owner.app.push_screen(MessageScreen(f"Could not extract {arc_name}:\n{res.error}"))
            else:
                owner.notify(f"Extracted {arc_name} ({res.files_processed} files)")

        owner.call_after_refresh(lambda: owner.run_worker(work, thread=True))

    owner.app.push_screen(ChoiceScreen(f"Extract {arc_name} to:", options, initial=0), on_choice)
