"""Fast parallel transfers for rclone FUSE mounts (e.g. /mnt/gdrive).

When the source or destination of a copy/move lives inside an `rclone mount`
filesystem (`fuse.rclone`), reading or writing thousands of small files one by
one through POSIX FUSE incurs one sequential HTTP round-trip per file. This
module maps the mounted path back to its `rclone` remote specification (e.g.
`/mnt/gdrive/folder` -> `gdrive:folder`) and delegates the transfer to a single
multi-threaded `rclone` process (`--transfers 16 --checkers 16`), streaming
live JSON progress back to CommanderTUI's progress bar and honouring `Esc`
cancellation via process-group signalling (`start_new_session=True` + `killpg`).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable

from .bookmarks import PROC_MOUNTS, _unescape_mount_path
from .models import OpKind
from .trash import send_to_trash

ProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]

DEFAULT_TRANSFERS = 16
DEFAULT_CHECKERS = 16
STATS_INTERVAL = "200ms"


@dataclass(frozen=True)
class RcloneMount:
    mountpoint: str
    remote: str


@dataclass
class RcloneResult:
    ok: bool
    files_transferred: int = 0
    bytes_transferred: int = 0
    error: str | None = None
    cancelled: bool = False


def rclone_available() -> bool:
    return shutil.which("rclone") is not None


def detect_rclone_mounts(proc_mounts: str = PROC_MOUNTS) -> list[RcloneMount]:
    """Return active rclone FUSE mounts ordered from most specific to least."""
    mounts: list[RcloneMount] = []
    if os.path.isfile(proc_mounts):
        try:
            with open(proc_mounts) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    device = _unescape_mount_path(parts[0])
                    mountpoint = os.path.normpath(_unescape_mount_path(parts[1]))
                    fstype = parts[2]
                    if fstype == "fuse.rclone" and ":" in device:
                        mounts.append(RcloneMount(mountpoint=mountpoint, remote=device))
        except OSError:
            pass
    elif sys.platform == "darwin":
        try:
            res = subprocess.run(["mount"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split(" on ", 1)
                    if len(parts) != 2:
                        continue
                    device = parts[0].strip()
                    path_part, _, fstype_part = parts[1].rpartition(" (")
                    if not fstype_part or ":" not in device or device.startswith("/"):
                        continue
                    if any(k in fstype_part.lower() for k in ("fuse", "osxfuse", "macfuse", "rclone")):
                        mounts.append(
                            RcloneMount(mountpoint=os.path.normpath(path_part.strip()), remote=device)
                        )
        except Exception:
            pass

    mounts.sort(key=lambda m: len(m.mountpoint), reverse=True)
    return mounts


def resolve_rclone_endpoint(path: str, mounts: list[RcloneMount] | None = None) -> tuple[str, bool]:
    """Translate a local path inside an rclone mount into `remote:subpath`.

    Returns `(spec_or_path, is_rclone)`.
    """
    if mounts is None:
        mounts = detect_rclone_mounts()
    norm = os.path.normpath(os.path.abspath(path))
    for m in mounts:
        if norm == m.mountpoint:
            return m.remote, True
        if norm.startswith(m.mountpoint + os.sep):
            rel = os.path.relpath(norm, m.mountpoint).replace(os.sep, "/")
            if m.remote.endswith(":"):
                return f"{m.remote}{rel}", True
            return f"{m.remote.rstrip('/')}/{rel}", True
    return norm, False


def is_rclone_transfer(src: str, dst: str, mounts: list[RcloneMount] | None = None) -> bool:
    """True when rclone is installed and either endpoint lives on an rclone mount."""
    if not rclone_available():
        return False
    if mounts is None:
        mounts = detect_rclone_mounts()
    if not mounts:
        return False
    _, src_rc = resolve_rclone_endpoint(src, mounts)
    _, dst_rc = resolve_rclone_endpoint(dst, mounts)
    return src_rc or dst_rc


def flush_mount_cache() -> None:
    """Send SIGHUP to active rclone mount daemons so directory caches refresh."""
    try:
        subprocess.run(
            ["pkill", "-HUP", "-x", "rclone"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except Exception:
        pass


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait(timeout=2)


def _run_rclone_process(
    cmd: list[str],
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    default_name: str = "",
) -> RcloneResult:
    if cancel_check and cancel_check():
        return RcloneResult(ok=False, error="cancelled", cancelled=True)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as e:
        return RcloneResult(ok=False, error=str(e))

    line_queue: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                line_queue.put(line)
        finally:
            line_queue.put(None)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    banked_bytes = 0
    in_flight_bytes = 0
    files_started = 0
    files_copied = 0
    last_bytes = 0
    current_name = default_name or "transferring..."
    errors: list[str] = []

    def _emit_progress(total_bytes_done: int, target_files: int, file_name: str, file_size: int) -> None:
        nonlocal banked_bytes, in_flight_bytes, files_started
        if not progress:
            return
        while files_started < target_files:
            banked_bytes += in_flight_bytes
            in_flight_bytes = 0
            files_started += 1
            progress(0, max(1, file_size), file_name)
        delta = max(0, total_bytes_done - banked_bytes)
        in_flight_bytes = delta
        if delta > 0:
            progress(delta, max(delta, file_size, 1), file_name)

    eof = False
    while not eof:
        if cancel_check and cancel_check():
            _terminate_process_group(proc)
            return RcloneResult(
                ok=False,
                files_transferred=files_copied,
                bytes_transferred=last_bytes,
                error="cancelled",
                cancelled=True,
            )
        try:
            line = line_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if line is None:
            eof = True
            break

        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        level = entry.get("level", "")
        msg = entry.get("msg", "")
        obj = entry.get("object", "")

        if obj:
            current_name = os.path.basename(obj) or obj

        if msg.startswith(("Copied ", "Moved ")):
            files_copied += 1
            if progress and files_started < files_copied:
                _emit_progress(last_bytes, files_copied, current_name, 1)

        stats = entry.get("stats")
        if isinstance(stats, dict):
            b_done = int(stats.get("bytes") or 0)
            last_bytes = max(last_bytes, b_done)
            transfers_done = int(stats.get("transfers") or 0)
            files_copied = max(files_copied, transfers_done)

            transferring = stats.get("transferring")
            cur_size = 1
            if isinstance(transferring, list) and transferring:
                first = transferring[0]
                if isinstance(first, dict):
                    t_name = first.get("name")
                    if t_name:
                        current_name = os.path.basename(t_name) or t_name
                    cur_size = int(first.get("size") or 1)

            target_files = max(files_copied, 1 if b_done > 0 else 0)
            _emit_progress(last_bytes, target_files, current_name, cur_size)

        if level in ("error", "critical") and not stats:
            err_text = f"{obj}: {msg}" if obj else msg
            if err_text and err_text not in errors:
                errors.append(err_text)

    rc = proc.wait()
    if cancel_check and cancel_check():
        return RcloneResult(
            ok=False,
            files_transferred=files_copied,
            bytes_transferred=last_bytes,
            error="cancelled",
            cancelled=True,
        )
    if rc != 0:
        err_msg = "; ".join(errors[:3]) if errors else f"rclone exited with code {rc}"
        return RcloneResult(
            ok=False,
            files_transferred=files_copied,
            bytes_transferred=last_bytes,
            error=err_msg,
        )
    return RcloneResult(
        ok=True,
        files_transferred=files_copied,
        bytes_transferred=last_bytes,
    )


def copy_with_rclone(
    src: str,
    dst: str,
    is_dir: bool,
    replace_existing: bool = True,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    mounts: list[RcloneMount] | None = None,
) -> RcloneResult:
    """Copy a directory tree or a single file using rclone."""
    if mounts is None:
        mounts = detect_rclone_mounts()
    src_spec, _ = resolve_rclone_endpoint(src, mounts)
    dst_spec, dst_is_rc = resolve_rclone_endpoint(dst, mounts)

    subcmd = "copy" if is_dir else "copyto"
    cmd = [
        "rclone",
        subcmd,
        src_spec,
        dst_spec,
        "--transfers",
        str(DEFAULT_TRANSFERS),
        "--checkers",
        str(DEFAULT_CHECKERS),
        "--stats",
        STATS_INTERVAL,
        "--use-json-log",
        "-v",
    ]
    if is_dir:
        cmd.append("--create-empty-src-dirs")
    if replace_existing:
        cmd.append("--ignore-times")

    res = _run_rclone_process(cmd, progress, cancel_check, default_name=os.path.basename(src))
    if res.ok and not is_dir and res.files_transferred == 0:
        res.files_transferred = 1
    if dst_is_rc:
        flush_mount_cache()
    return res


def move_with_rclone(
    src: str,
    dst: str,
    is_dir: bool,
    replace_existing: bool = True,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    mounts: list[RcloneMount] | None = None,
) -> RcloneResult:
    """Move a directory tree or a single file using rclone.

    - If `src` is on a local filesystem and `dst` is on an rclone mount, copies
      to the cloud first and then sends the local `src` to the OS trash
      (`send_to_trash`), preserving CommanderTUI's trash guarantee.
    - If `src` is on an rclone mount, uses `rclone move`/`moveto` (which moves
      source files to cloud trash on backends like Google Drive).
    """
    if mounts is None:
        mounts = detect_rclone_mounts()
    src_spec, src_is_rc = resolve_rclone_endpoint(src, mounts)
    dst_spec, dst_is_rc = resolve_rclone_endpoint(dst, mounts)

    if not src_is_rc:
        res = copy_with_rclone(src, dst, is_dir, replace_existing, progress, cancel_check, mounts)
        if not res.ok or res.cancelled:
            return res
        if not send_to_trash(src):
            return RcloneResult(
                ok=False,
                files_transferred=res.files_transferred,
                bytes_transferred=res.bytes_transferred,
                error="Copied to destination but could not trash local source",
            )
        return res

    subcmd = "move" if is_dir else "moveto"
    cmd = [
        "rclone",
        subcmd,
        src_spec,
        dst_spec,
        "--transfers",
        str(DEFAULT_TRANSFERS),
        "--checkers",
        str(DEFAULT_CHECKERS),
        "--stats",
        STATS_INTERVAL,
        "--use-json-log",
        "-v",
    ]
    if is_dir:
        cmd.extend(["--create-empty-src-dirs", "--delete-empty-src-dirs"])
    if replace_existing:
        cmd.append("--ignore-times")

    res = _run_rclone_process(cmd, progress, cancel_check, default_name=os.path.basename(src))
    if res.ok and not is_dir and res.files_transferred == 0:
        res.files_transferred = 1
    if res.ok and is_dir:
        # Remove the top-level directory on the remote if now empty.
        subprocess.run(
            ["rclone", "rmdir", src_spec],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    if src_is_rc or dst_is_rc:
        flush_mount_cache()
    return res


def batch_transfer_files_with_rclone(
    rel_paths: list[str],
    src_root: str,
    dst_root: str,
    kind: OpKind,
    replace_existing: bool = True,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    mounts: list[RcloneMount] | None = None,
) -> RcloneResult:
    """Transfer a list of relative file paths from `src_root` to `dst_root` in
    one parallel `rclone` invocation via `--files-from-raw`."""
    if not rel_paths:
        return RcloneResult(ok=True)
    if mounts is None:
        mounts = detect_rclone_mounts()
    src_spec, src_is_rc = resolve_rclone_endpoint(src_root, mounts)
    dst_spec, dst_is_rc = resolve_rclone_endpoint(dst_root, mounts)

    use_rclone_move = kind == OpKind.MOVE and src_is_rc
    subcmd = "move" if use_rclone_move else "copy"

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tf:
        list_file = tf.name
        for rel in rel_paths:
            tf.write(rel.replace(os.sep, "/") + "\n")

    try:
        cmd = [
            "rclone",
            subcmd,
            src_spec,
            dst_spec,
            "--files-from-raw",
            list_file,
            "--no-traverse",
            "--transfers",
            str(DEFAULT_TRANSFERS),
            "--checkers",
            str(DEFAULT_CHECKERS),
            "--stats",
            STATS_INTERVAL,
            "--use-json-log",
            "-v",
        ]
        if replace_existing:
            cmd.append("--ignore-times")
        res = _run_rclone_process(cmd, progress, cancel_check, default_name=os.path.basename(rel_paths[0]))
    finally:
        try:
            os.remove(list_file)
        except OSError:
            pass

    if res.ok and res.files_transferred == 0:
        res.files_transferred = len(rel_paths)

    if res.ok and kind == OpKind.MOVE and not src_is_rc:
        for rel in rel_paths:
            local_src = os.path.join(src_root, rel)
            if os.path.lexists(local_src) and not send_to_trash(local_src):
                return RcloneResult(
                    ok=False,
                    files_transferred=res.files_transferred,
                    bytes_transferred=res.bytes_transferred,
                    error=f"Copied but could not trash source: {local_src}",
                )

    if src_is_rc or dst_is_rc:
        flush_mount_cache()
    return res
