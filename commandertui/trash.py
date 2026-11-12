from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from urllib.parse import quote, unquote

from . import bookmarks


def _fallback_trash_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/.Trash")
    return os.path.expanduser("~/.local/share/Trash/files")


LOCAL_FILESYSTEMS = {
    "ext2",
    "ext3",
    "ext4",
    "btrfs",
    "xfs",
    "zfs",
    "f2fs",
    "vfat",
    "exfat",
    "ntfs",
    "ntfs3",
    "fuseblk",
    "hfs",
    "hfsplus",
    "apfs",
    "tmpfs",
}


def _containing_mount(path: str) -> tuple[str, str, str] | None:
    # Resolve parent symlinks, but classify the link itself, not its target.
    absolute = os.path.abspath(path)
    candidate = os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))
    candidate = os.path.normpath(candidate)
    mounts = []
    try:
        if sys.platform == "darwin":
            result = subprocess.run(["mount"], capture_output=True, text=True, timeout=5, check=True)
            for line in result.stdout.splitlines():
                match = re.match(r"(.+) on (.+) \(([^, )]+)", line)
                if match:
                    source, target, filesystem = match.groups()
                    mounts.append((source, bookmarks._unescape_mount_path(target), filesystem))
        else:
            with open(bookmarks.PROC_MOUNTS) as stream:
                for line in stream:
                    fields = line.split()
                    if len(fields) >= 3:
                        mounts.append((fields[0], bookmarks._unescape_mount_path(fields[1]), fields[2]))
    except (OSError, subprocess.SubprocessError):
        return None  # Unknown storage is never assumed to be a local disk.
    containing = [
        m for m in mounts if candidate == m[1] or candidate.startswith(m[1].rstrip(os.sep) + os.sep)
    ]
    return max(containing, key=lambda m: len(m[1]), default=None)


def is_remote_path(path: str) -> bool:
    """Only known local disk filesystems (or Linux block-device mounts) are local."""
    mount = _containing_mount(path)
    if mount is None:
        return True
    source, target, filesystem = mount
    local = filesystem in LOCAL_FILESYSTEMS or (filesystem == "overlay" and target == "/")
    if sys.platform != "darwin" and source.startswith("/dev/") and not filesystem.startswith("fuse."):
        local = True
    return not local


def remote_location(path: str) -> str:
    mount = _containing_mount(path)
    if mount is None:
        return os.path.dirname(os.path.abspath(path))
    _, target, filesystem = mount
    if filesystem == "fuse.gvfsd-fuse":
        # GVFS mounts every connection below one FUSE mount. Use its first
        # relative component, never a filename elsewhere that resembles a URI.
        absolute = os.path.abspath(path)
        location_path = os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))
        relative = os.path.relpath(location_path, target)
        entry = relative.split(os.sep, 1)[0]
        backend, _, raw = entry.partition(":")
        params = dict(pair.split("=", 1) for pair in raw.split(",") if "=" in pair)
        host = unquote(params.get("server") or params.get("host") or "")
        schemes = {"smb-share": "smb", "sftp": "sftp", "ftp": "ftp", "dav": "dav", "davs": "davs"}
        scheme = schemes.get(backend)
        if scheme and host:
            if scheme == "dav" and params.get("ssl", "").lower() in {"true", "1"}:
                scheme = "davs"
            # URI authority needs brackets around IPv6 literals.
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = params.get("port", "")
            if port.isdigit():
                host += f":{port}"
            share = unquote(params.get("share") or params.get("prefix") or "")
            suffix = "/" + quote(share.lstrip("/"), safe="/") if share else ""
            return f"{scheme}://{host}{suffix}"
    label = filesystem.removeprefix("fuse.")
    return f"{target} ({label})"


def remove_path(path: str, local_trash=None) -> bool:
    """Remove a previously approved item: remote permanently, local via trash.

    Callers must confirm remote DELETE/Replace beforehand. MOVE already authorizes
    source removal after the copy has been verified. Directory symlinks are unlinked.
    """
    if not is_remote_path(path):
        return (local_trash or send_to_trash)(path)
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return True
    except OSError:
        return False


def send_to_trash(path: str) -> bool:
    """Trash a local item, never copying it across filesystems.

    Three-layer fallback, in order of preference:
      1. send2trash (FreeDesktop-native, cross-desktop)
      2. `gio trash` (GLib/GIO, present on GNOME/KDE)
      3. Same-filesystem rename to the local trash; EXDEV fails without copying.
    """
    abs_path = os.path.abspath(path)
    if is_remote_path(abs_path) or not os.path.lexists(abs_path):
        return False

    try:
        import send2trash

        send2trash.send2trash(abs_path)
        return True
    except Exception:
        pass

    try:
        result = subprocess.run(["gio", "trash", abs_path], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return True
    except Exception:
        pass

    try:
        trash_dir = _fallback_trash_dir()
        os.makedirs(trash_dir, exist_ok=True)
        base = os.path.basename(abs_path)
        name, ext = os.path.splitext(base)
        target = os.path.join(trash_dir, base)
        counter = 1
        while os.path.lexists(target):
            target = os.path.join(trash_dir, f"{name}_{counter}{ext}")
            counter += 1
        os.rename(abs_path, target)
        return True
    except Exception:
        return False
