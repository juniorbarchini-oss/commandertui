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
from dataclasses import dataclass, field
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
    failed: list[tuple[str, str]] = field(default_factory=list)
    succeeded: list[str] = field(default_factory=list)

    @property
    def is_config_error(self) -> bool:
        return is_rclone_config_error(self.error)


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


def is_rclone_config_error(error_msg: str | None) -> bool:
    """Return True if the error indicates a missing rclone remote/config rather than
    a real transfer failure."""
    if not error_msg:
        return False
    lower = error_msg.lower()
    patterns = (
        "didn't find section in config file",
        "did not find section in config file",
        "couldn't find remote",
        "could not find remote",
        "failed to create file system for",
        "config file",
    )
    return any(p in lower for p in patterns)


def tree_has_symlinks(path: str) -> bool:
    """Return True if `path` is a symlink or contains any symlink within."""
    try:
        if os.path.islink(path):
            return True
        if not os.path.isdir(path):
            return False
        for root, dirs, files in os.walk(path, followlinks=False):
            for d in dirs:
                if os.path.islink(os.path.join(root, d)):
                    return True
            for f in files:
                if os.path.islink(os.path.join(root, f)):
                    return True
    except OSError:
        pass
    return False


def affected_mountpoints(paths_or_specs: list[str], mounts: list[RcloneMount]) -> list[str]:
    """Return mountpoints from `mounts` that contain any of `paths_or_specs`."""
    affected: set[str] = set()
    for item in paths_or_specs:
        norm = os.path.normpath(os.path.abspath(item))
        for m in mounts:
            if norm == m.mountpoint or norm.startswith(m.mountpoint + os.sep):
                affected.add(m.mountpoint)
            elif m.remote and (item == m.remote or item.startswith(m.remote)):
                affected.add(m.mountpoint)
    return sorted(affected)


def find_rclone_mount_pids(
    mountpoints: list[str] | None = None,
    proc_dir: str = "/proc",
) -> list[int]:
    """Find PIDs of active `rclone mount` processes owned by the current user
    matching any of the specified `mountpoints`.

    If `mountpoints` is None or empty, returns all `rclone mount` processes of the user.
    """
    matched_pids: list[int] = []
    norm_targets = [os.path.normpath(os.path.abspath(m)) for m in mountpoints] if mountpoints else []

    if os.path.isdir(proc_dir):
        uid = os.getuid() if hasattr(os, "getuid") else None
        try:
            entries = os.listdir(proc_dir)
        except OSError:
            entries = []

        for entry in entries:
            if not entry.isdigit():
                continue
            pid_dir = os.path.join(proc_dir, entry)
            # In Linux real /proc, verify ownership to avoid trying to signal other users
            if proc_dir == "/proc" and uid is not None:
                try:
                    if os.stat(pid_dir).st_uid != uid:
                        continue
                except OSError:
                    continue

            cmdline_path = os.path.join(pid_dir, "cmdline")
            try:
                with open(cmdline_path, "rb") as f:
                    raw = f.read()
            except OSError:
                continue

            if not raw:
                continue

            if b"\x00" in raw:
                parts = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
            else:
                parts = [p.decode("utf-8", "replace") for p in raw.split() if p]

            if not parts:
                continue

            prog = os.path.basename(parts[0])
            if prog != "rclone":
                continue

            args = parts[1:]
            if "mount" not in args:
                continue

            if norm_targets:
                has_target = False
                for arg in args:
                    norm_arg = (
                        os.path.normpath(os.path.abspath(arg))
                        if os.path.isabs(arg)
                        else os.path.normpath(arg)
                    )
                    if norm_arg in norm_targets or os.path.normpath(arg) in norm_targets:
                        has_target = True
                        break
                if not has_target:
                    continue

            matched_pids.append(int(entry))
    elif sys.platform == "darwin":
        try:
            uid = os.getuid() if hasattr(os, "getuid") else None
            cmd = ["pgrep", "-f", "rclone mount"]
            if uid is not None:
                cmd = ["pgrep", "-u", str(uid), "-f", "rclone mount"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    pid_str = line.strip()
                    if not pid_str.isdigit():
                        continue
                    pid = int(pid_str)
                    if norm_targets:
                        ps_res = subprocess.run(
                            ["ps", "-p", str(pid), "-o", "command="],
                            capture_output=True,
                            text=True,
                            timeout=3,
                            check=False,
                        )
                        if ps_res.returncode == 0:
                            ps_cmd = ps_res.stdout.strip()
                            if not any(target in ps_cmd for target in norm_targets):
                                continue
                    matched_pids.append(pid)
        except Exception:
            pass

    return sorted(matched_pids)


def flush_mount_cache(
    mountpoints: list[str] | None = None,
    proc_dir: str = "/proc",
) -> list[int]:
    """Send SIGHUP only to active `rclone mount` processes matching `mountpoints`."""
    signalled: list[int] = []
    pids = find_rclone_mount_pids(mountpoints, proc_dir=proc_dir)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGHUP)
            signalled.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    return signalled


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
    failed_items: dict[str, str] = {}
    succeeded_set: set[str] = set()
    succeeded_list: list[str] = []

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
                failed=list(failed_items.items()),
                succeeded=list(succeeded_list),
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

        if msg.startswith(("Copied", "Moved")):
            files_copied += 1
            if obj:
                if obj not in succeeded_set:
                    succeeded_set.add(obj)
                    succeeded_set.add(os.path.normpath(obj))
                    succeeded_list.append(obj)
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
            if obj:
                failed_items[obj] = msg or "error"

    rc = proc.wait()
    if cancel_check and cancel_check():
        return RcloneResult(
            ok=False,
            files_transferred=files_copied,
            bytes_transferred=last_bytes,
            error="cancelled",
            cancelled=True,
            failed=list(failed_items.items()),
            succeeded=list(succeeded_list),
        )
    if rc == 0:
        return RcloneResult(
            ok=True,
            files_transferred=files_copied,
            bytes_transferred=last_bytes,
            failed=[],
            succeeded=list(succeeded_list),
        )
    active_failed = [
        (obj, err)
        for obj, err in failed_items.items()
        if obj not in succeeded_set and os.path.normpath(obj) not in succeeded_set
    ]
    err_msg = "; ".join(errors[:3]) if errors else f"rclone exited with code {rc}"
    return RcloneResult(
        ok=False,
        files_transferred=files_copied,
        bytes_transferred=last_bytes,
        error=err_msg,
        failed=active_failed,
        succeeded=list(succeeded_list),
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
        flush_mount_cache(affected_mountpoints([dst], mounts))
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
        flush_mount_cache(affected_mountpoints([src, dst], mounts))
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

    if kind == OpKind.MOVE and not src_is_rc:
        if not res.cancelled:
            if res.ok:
                to_trash = rel_paths
            else:
                succeeded_set = {os.path.normpath(name) for name in res.succeeded}
                to_trash = [rel for rel in rel_paths if os.path.normpath(rel) in succeeded_set]

            for rel in to_trash:
                local_src = os.path.join(src_root, rel)
                if os.path.lexists(local_src) and not send_to_trash(local_src):
                    res.ok = False
                    res.failed.append((rel, f"Copied but could not trash source: {local_src}"))

    if src_is_rc or dst_is_rc:
        flush_mount_cache(affected_mountpoints([src_root, dst_root], mounts))
    return res
