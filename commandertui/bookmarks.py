from __future__ import annotations

import json
import os
from dataclasses import dataclass

CONFIG_DIR = os.path.expanduser("~/.config/commandertui")
BOOKMARKS_FILE = os.path.join(CONFIG_DIR, "bookmarks.json")

# Where Linux desktops mount removable/network drives.
MOUNT_ROOTS = ("/run/media", "/media", "/mnt")

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
                if not user_dir.is_dir():
                    continue
                # /run/media/<user>/<Label> vs /mnt/<Label> (no user subdir)
                candidates = (
                    list(os.scandir(user_dir.path))
                    if root == "/run/media"
                    else [user_dir]
                )
                for c in candidates:
                    if c.is_dir():
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
    return [home] + detect_mounted() + detect_network_mounts() + load_saved()
