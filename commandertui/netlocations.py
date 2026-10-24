"""Network locations (SMB / SFTP / FTP).

On Linux this uses GVFS (`gio`) -- the same engine Nautilus uses.
On macOS this uses native SMB (`smbutil view` + `osascript mount volume` into `/Volumes`).
Passwords are never handled here: when a server needs a login, `gio` (Linux) or
macOS Keychain asks for it itself and remembers it if the user chooses.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote

from .bookmarks import CONFIG_DIR

NETWORK_FILE = os.path.join(CONFIG_DIR, "network.json")
SCHEMES = ("smb", "sftp", "ftp")
GIO_TIMEOUT = 30
DARWIN_MOUNT_WAIT = 30.0
DARWIN_POLL_INTERVAL = 0.25
MACOS_UNSUPPORTED_MSG = "SFTP and FTP network locations are not supported on macOS (only SMB is available)."


@dataclass(frozen=True)
class NetLocation:
    label: str
    uri: str
    user: str | None = None


def is_macos() -> bool:
    return sys.platform == "darwin"


def gio_available() -> bool:
    if is_macos():
        return shutil.which("smbutil") is not None and shutil.which("osascript") is not None
    return shutil.which("gio") is not None


def network_available() -> bool:
    return gio_available()


def build_uri(scheme: str, host: str, share: str | None = None, user: str | None = None) -> str:
    host = host.strip().strip("/")
    user_clean = user.strip() if user else ""
    auth = f"{quote(unquote(user_clean), safe='')}@" if user_clean else ""
    uri = f"{scheme}://{auth}{host}/"
    if share:
        share_clean = unquote(share.strip("/"))
        encoded_share = quote(share_clean, safe="") if user_clean else share.strip("/")
        uri += f"{encoded_share}/"
    return uri


def is_uri(value: str) -> bool:
    return any(value.startswith(f"{s}://") for s in SCHEMES)


def _split_uri(uri: str) -> tuple[str, str, str, str]:
    scheme, _, rest = uri.partition("://")
    host_part, _, share_part = rest.strip("/").partition("/")
    if "@" in host_part:
        raw_user, _, host = host_part.rpartition("@")
        user = unquote(raw_user)
    else:
        user, host = "", host_part
    return scheme.lower(), user, host, unquote(share_part.strip("/"))


def label_for(uri: str) -> str:
    scheme, _, host, share = _split_uri(uri)
    return f"{share} @ {host} ({scheme})" if share else f"{host} ({scheme})"


def _gio(args: list[str], interactive: bool = False) -> subprocess.CompletedProcess:
    """Non-interactive by default (stdin closed): succeeds silently when the
    keyring already has the credentials. interactive=True inherits the real
    terminal so gio can ask for user/password itself."""
    if interactive:
        return subprocess.run(["gio", *args], timeout=None)
    return subprocess.run(
        ["gio", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIO_TIMEOUT
    )


def _parse_smbutil_view(output: str) -> list[str]:
    """Extract Disk share names from `smbutil view` output."""
    shares: list[str] = []
    in_table = False
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("---"):
            in_table = True
            continue
        if not in_table:
            continue
        if line.endswith("shares listed") or line.endswith("share listed"):
            break
        # Columns: Share (padded to 48 chars), Type ("Disk", "Pipe", "Printer"...), Comments
        for marker in ("Disk", "Pipe", "Printer", "Comm", "IPC"):
            idx = raw.find(marker)
            if idx > 0 and raw[idx - 1].isspace():
                after = raw[idx + len(marker) :]
                if not after or after[0].isspace():
                    if marker == "Disk":
                        name = raw[:idx].strip()
                        if name and not name.endswith("$"):
                            shares.append(name)
                    break
    return shares


def _list_shares_darwin(host: str, user: str | None = None) -> tuple[list[str] | None, str]:
    host_clean = host.strip().strip("/")
    if not user and "@" in host_clean:
        raw_user, _, host_clean = host_clean.rpartition("@")
        user = unquote(raw_user)
    user_clean = user.strip() if user else ""
    auth = f"{quote(unquote(user_clean), safe='')}@" if user_clean else ""
    target = f"//{auth}{host_clean}"
    try:
        res = subprocess.run(
            ["smbutil", "view", "-N", target],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIO_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except OSError as e:
        return None, str(e)
    if res.returncode != 0:
        err = (res.stderr or res.stdout).strip()
        return None, err or "could not list shares"
    return _parse_smbutil_view(res.stdout), ""


def list_shares(host: str, user: str | None = None) -> tuple[list[str] | None, str]:
    """Shares on an SMB server. Returns (names, "") or (None, error) -- the
    caller retries after an interactive mount if login was the problem."""
    if is_macos():
        return _list_shares_darwin(host, user=user)
    try:
        res = _gio(["list", build_uri("smb", host)])
    except subprocess.TimeoutExpired:
        return None, "timed out"
    if res.returncode != 0:
        return None, res.stderr.strip()
    return [n for n in res.stdout.splitlines() if n.strip()], ""


def _darwin_share_mount_path(host: str, share: str) -> str | None:
    """Return the real mount point (e.g. /Volumes/HD1 or /Volumes/HD1-1) for
    the SMB share on `host` by inspecting `/sbin/mount -t smbfs`."""
    clean_host = host.strip().strip("/").lower()
    clean_share = unquote(share.strip("/")).lower()
    if not clean_host or not clean_share:
        return None
    try:
        res = subprocess.run(
            ["/sbin/mount", "-t", "smbfs"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIO_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        # Format: //user@host/share on /Volumes/NAME (smbfs, nodev, nosuid, mounted by user)
        parts = line.split(" on ", 1)
        if len(parts) != 2 or not parts[0].startswith("//"):
            continue
        remote = parts[0][2:]
        host_part, _, share_part = remote.partition("/")
        _, _, m_host = host_part.rpartition("@")
        m_host = unquote(m_host.strip()).lower()
        # Strip optional :port if present
        if ":" in m_host and not m_host.startswith("["):
            m_host = m_host.split(":", 1)[0]
        m_share = unquote(share_part.strip("/")).lower()
        if m_host == clean_host and m_share == clean_share:
            mount_point, _, _ = parts[1].rpartition(" (")
            mount_point = mount_point.strip()
            if mount_point:
                return mount_point
    return None


def _osascript_mount(uri: str) -> tuple[bool, str]:
    escaped = uri.replace("\\", "\\\\").replace('"', '\\"')
    try:
        res = subprocess.run(
            ["osascript", "-e", f'mount volume "{escaped}"'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIO_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as e:
        return False, str(e)
    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip()
    return True, ""


def _mount_darwin(uri: str, interactive: bool = False) -> tuple[bool, str]:
    scheme, user, host, share = _split_uri(uri)
    if scheme != "smb":
        return False, MACOS_UNSUPPORTED_MSG
    if not host:
        return False, "missing host"
    if share and _darwin_share_mount_path(host, share):
        return True, ""
    if not user:
        return False, "SMB user is required on macOS"
    if not share and not interactive:
        return False, "authentication required"
    encoded_user = quote(user, safe="")
    if share:
        encoded_share = quote(share, safe="")
        mount_uri = f"smb://{encoded_user}@{host}/{encoded_share}"
    else:
        mount_uri = f"smb://{encoded_user}@{host}"
    ok, err = _osascript_mount(mount_uri)
    if not ok:
        return False, err
    if not share:
        return True, ""
    deadline = time.monotonic() + DARWIN_MOUNT_WAIT
    while time.monotonic() < deadline:
        if _darwin_share_mount_path(host, share):
            return True, ""
        time.sleep(DARWIN_POLL_INTERVAL)
    return False, f"timed out waiting for /Volumes/{share}"


def mount(uri: str, interactive: bool = False) -> tuple[bool, str]:
    if is_macos():
        return _mount_darwin(uri, interactive=interactive)
    try:
        res = _gio(["mount", uri], interactive)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    if res.returncode == 0:
        return True, ""
    if not interactive and "already mounted" in res.stderr:
        return True, ""
    return False, "" if interactive else (res.stderr.strip() or res.stdout.strip())


def local_path(uri: str) -> str | None:
    """The local directory behind a mounted URI (/run/user/UID/gvfs/... on Linux,
    /Volumes/<share> on macOS)."""
    if is_macos():
        scheme, _, host, share = _split_uri(uri)
        if scheme != "smb" or not host or not share:
            return None
        return _darwin_share_mount_path(host, share)
    if uri.startswith("/") and os.path.isdir(uri):
        return uri
    try:
        res = _gio(["info", "-a", "standard::name", uri])
    except subprocess.TimeoutExpired:
        return None
    for line in res.stdout.splitlines():
        if line.startswith("local path:"):
            return line.split(":", 1)[1].strip()
    return None


def is_smb_mount_point(path: str) -> bool:
    """Check if `path` (e.g. /Volumes/HD1) is a mounted smbfs volume on macOS."""
    if not is_macos():
        return False
    clean_path = os.path.abspath(path).rstrip(os.sep).lower()
    try:
        res = subprocess.run(
            ["/sbin/mount", "-t", "smbfs"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIO_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if res.returncode != 0:
        return False
    for line in res.stdout.splitlines():
        parts = line.split(" on ", 1)
        if len(parts) != 2:
            continue
        mount_point, _, _ = parts[1].rpartition(" (")
        mount_point = os.path.abspath(mount_point.strip()).rstrip(os.sep).lower()
        if mount_point == clean_path:
            return True
    return False


def unmount(target: str) -> tuple[bool, str]:
    """Unmount a mounted SMB network location (`diskutil unmount` on macOS,
    `gio mount -u` on Linux). `target` can be an SMB URI or a local mount path."""
    if is_macos():
        if target.startswith("/"):
            mounted = target if is_smb_mount_point(target) else None
        else:
            scheme, _, _, _ = _split_uri(target)
            if scheme != "smb":
                return False, "Not a mounted network location"
            mounted = local_path(target)
        if not mounted:
            return False, "Not a mounted network location"
        try:
            res = subprocess.run(
                ["diskutil", "unmount", mounted],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=GIO_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, "timed out"
        except OSError as e:
            return False, str(e)
        if res.returncode == 0:
            return True, ""
        return False, (res.stderr or res.stdout).strip()
    try:
        res = _gio(["mount", "-u", target])
    except subprocess.TimeoutExpired:
        return False, "timed out"
    if res.returncode == 0:
        return True, ""
    return False, (res.stderr.strip() or res.stdout.strip())


def load_locations() -> list[NetLocation]:
    try:
        with open(NETWORK_FILE) as f:
            items = []
            for d in json.load(f):
                uri = d["uri"]
                user = d.get("user") or _split_uri(uri)[1] or None
                items.append(NetLocation(label=d["label"], uri=uri, user=user))
            return items
    except (OSError, ValueError, KeyError):
        return []


def _write(locations: list[NetLocation]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    rows = []
    for loc in locations:
        scheme, uri_user, host, share = _split_uri(loc.uri)
        row = {"label": loc.label, "uri": loc.uri, "protocol": scheme, "host": host, "share": share}
        user = loc.user or uri_user
        if user:
            row["user"] = user
        rows.append(row)
    with open(NETWORK_FILE, "w") as f:
        json.dump(rows, f, indent=2)


def save_location(uri: str, user: str | None = None) -> None:
    _, uri_user, _, _ = _split_uri(uri)
    effective_user = user or uri_user or None
    locations = [loc for loc in load_locations() if loc.uri != uri]
    locations.append(NetLocation(label=label_for(uri), uri=uri, user=effective_user))
    _write(locations)


def remove_location(uri: str) -> None:
    _write([loc for loc in load_locations() if loc.uri != uri])
