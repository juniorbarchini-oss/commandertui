from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

CONFIG_DIR = os.path.expanduser("~/.config/commandertui")
BOOKMARKS_FILE = os.path.join(CONFIG_DIR, "bookmarks.json")

# Where OS mounts removable/network drives.
# Linux uses /run/media, /media, /mnt; macOS uses /Volumes.
if sys.platform == "darwin":
    MOUNT_ROOTS = ("/Volumes",)
else:
    MOUNT_ROOTS = ("/run/media", "/media", "/mnt")

# macOS system volumes to exclude from Places (macOS only: on Linux a drive
# labelled "VM" or "Update" is a perfectly normal drive and must show up).
MACOS_EXCLUDED_VOLUMES = {"Recovery", "VM", "Preboot", "Update"}


def _hidden_volume(name: str) -> bool:
    if sys.platform != "darwin":
        return False
    return name.startswith((".", "com.apple.")) or name in MACOS_EXCLUDED_VOLUMES


# Nautilus (and GNOME apps generally) mount network shares -- SMB, FTP,
# SFTP -- through GVFS rather than udisks2, so they never show up under
# MOUNT_ROOTS. They land here instead, one FUSE directory per connection.
GVFS_ROOT = f"/run/user/{os.getuid()}/gvfs"


@dataclass(frozen=True)
class Place:
    label: str
    path: str
    kind: str  # "mounted" | "saved" | "home"


def detect_mounted() -> list[Place]:
    places: list[Place] = []
    for root in MOUNT_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            for user_dir in os.scandir(root):
                if _hidden_volume(user_dir.name):
                    continue
                if not user_dir.is_dir():
                    continue
                # /run/media/<user>/<Label> vs /mnt/<Label> or /Volumes/<Label> (no user subdir)
                candidates = list(os.scandir(user_dir.path)) if root == "/run/media" else [user_dir]
                for c in candidates:
                    if _hidden_volume(c.name):
                        continue
                    # An on-demand mount point (e.g. /mnt/i7server) exists as
                    # an empty directory even when nothing is mounted there --
                    # os.path.ismount() is what actually tells them apart.
                    if c.is_dir() and os.path.ismount(c.path):
                        places.append(Place(label=c.name, path=c.path, kind="mounted"))
        except PermissionError:
            continue
    return places


def _label_for_gvfs_entry(name: str) -> str:
    """GVFS names a mount after its full connection URI, e.g.
    'smb-share:server=192.168.1.10,share=Media' -- turn that into something
    a human would recognize, falling back to the raw name if it's a shape
    we don't specifically parse (sftp, ftp, google-drive, ...).
    """
    _scheme, _, rest = name.partition(":")
    params = dict(pair.split("=", 1) for pair in rest.split(",") if "=" in pair)
    share = params.get("share")
    server = params.get("server") or params.get("host")
    if share and server:
        return f"{share} @ {server}"
    return server or name


def detect_network_mounts() -> list[Place]:
    if not os.path.isdir(GVFS_ROOT):
        return []
    try:
        return [
            Place(label=_label_for_gvfs_entry(entry.name), path=entry.path, kind="network")
            for entry in os.scandir(GVFS_ROOT)
            if entry.is_dir()
        ]
    except PermissionError:
        return []


PROC_MOUNTS = "/proc/mounts"


def _unescape_mount_path(path: str) -> str:
    # /proc/mounts octal-escapes whitespace and backslashes in paths.
    return path.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def _detect_home_mounts_proc() -> list[Place]:
    home = os.path.expanduser("~")
    try:
        with open(PROC_MOUNTS) as f:
            lines = f.readlines()
    except OSError:
        return []
    places: list[Place] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or not parts[2].startswith("fuse."):
            continue
        path = _unescape_mount_path(parts[1])
        if path.startswith(home + os.sep):
            places.append(Place(label=os.path.basename(path), path=path, kind=parts[2][5:]))
    return places


def _detect_home_mounts_darwin() -> list[Place]:
    home = os.path.expanduser("~")
    places: list[Place] = []
    try:
        res = subprocess.run(["mount"], capture_output=True, text=True, timeout=5)
        if res.returncode != 0:
            return []
        for line in res.stdout.splitlines():
            # e.g.: remote: on /Users/user/365 (macfuse, nodev, ...)
            parts = line.split(" on ")
            if len(parts) < 2:
                continue
            rest = parts[1]
            path, _, fstype_part = rest.rpartition(" (")
            if not fstype_part:
                continue
            fstype = fstype_part.rstrip(")").split(",")[0].strip()
            path = path.strip()
            if path.startswith(home + os.sep) and any(
                f in fstype.lower() for f in ("fuse", "osxfuse", "macfuse", "rclone")
            ):
                places.append(Place(label=os.path.basename(path), path=path, kind=fstype))
    except Exception:
        pass
    return places


def detect_home_mounts() -> list[Place]:
    """FUSE mounts inside $HOME, e.g. rclone's ~/365 and ~/GoogleDrive.
    Read from /proc/mounts on Linux (or if PROC_MOUNTS exists) or mount on macOS."""
    if os.path.isfile(PROC_MOUNTS):
        return _detect_home_mounts_proc()
    if sys.platform == "darwin":
        return _detect_home_mounts_darwin()
    return []


def load_saved() -> list[Place]:
    if not os.path.isfile(BOOKMARKS_FILE):
        return []
    try:
        with open(BOOKMARKS_FILE) as f:
            data = json.load(f)
        return [Place(label=b["label"], path=b["path"], kind="saved") for b in data]
    except Exception:
        return []


def save_bookmark(label: str, path: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    saved = load_saved()
    saved = [b for b in saved if b.path != path]
    saved.append(Place(label=label, path=path, kind="saved"))
    with open(BOOKMARKS_FILE, "w") as f:
        json.dump([{"label": b.label, "path": b.path} for b in saved], f, indent=2)


def remove_bookmark(path: str) -> None:
    saved = [b for b in load_saved() if b.path != path]
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(BOOKMARKS_FILE, "w") as f:
        json.dump([{"label": b.label, "path": b.path} for b in saved], f, indent=2)


def all_places() -> list[Place]:
    home = Place(label="Home", path=os.path.expanduser("~"), kind="home")
    return [home] + detect_mounted() + detect_network_mounts() + detect_home_mounts() + load_saved()
