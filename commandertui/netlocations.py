"""Network locations (SMB / SFTP / FTP) through GVFS -- the same engine
Nautilus uses. Passwords are never handled here: when a server needs a login,
`gio` asks for it itself and stores it in the system keyring (libsecret) if
the user chooses to remember it, exactly like Nautilus does.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from .bookmarks import CONFIG_DIR

NETWORK_FILE = os.path.join(CONFIG_DIR, "network.json")
SCHEMES = ("smb", "sftp", "ftp")
GIO_TIMEOUT = 30


@dataclass(frozen=True)
class NetLocation:
    label: str
    uri: str


def gio_available() -> bool:
    return shutil.which("gio") is not None


def build_uri(scheme: str, host: str, share: str | None = None) -> str:
    host = host.strip().strip("/")
    uri = f"{scheme}://{host}/"
    if share:
        uri += share.strip("/") + "/"
    return uri


def is_uri(value: str) -> bool:
    return any(value.startswith(f"{s}://") for s in SCHEMES)


def label_for(uri: str) -> str:
    scheme, _, rest = uri.partition("://")
    host, _, share = rest.strip("/").partition("/")
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


def list_shares(host: str) -> tuple[list[str] | None, str]:
    """Shares on an SMB server. Returns (names, "") or (None, error) -- the
    caller retries after an interactive mount if login was the problem."""
    try:
        res = _gio(["list", build_uri("smb", host)])
    except subprocess.TimeoutExpired:
        return None, "timed out"
    if res.returncode != 0:
        return None, res.stderr.strip()
    return [n for n in res.stdout.splitlines() if n.strip()], ""


def mount(uri: str, interactive: bool = False) -> tuple[bool, str]:
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
    """The /run/user/UID/gvfs/... directory behind a mounted URI."""
    try:
        res = _gio(["info", "-a", "standard::name", uri])
    except subprocess.TimeoutExpired:
        return None
    for line in res.stdout.splitlines():
        if line.startswith("local path:"):
            return line.split(":", 1)[1].strip()
    return None


def load_locations() -> list[NetLocation]:
    try:
        with open(NETWORK_FILE) as f:
            return [NetLocation(label=d["label"], uri=d["uri"]) for d in json.load(f)]
    except (OSError, ValueError, KeyError):
        return []


def _write(locations: list[NetLocation]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(NETWORK_FILE, "w") as f:
        json.dump([{"label": loc.label, "uri": loc.uri} for loc in locations], f, indent=2)


def save_location(uri: str) -> None:
    locations = [loc for loc in load_locations() if loc.uri != uri]
    locations.append(NetLocation(label=label_for(uri), uri=uri))
    _write(locations)


def remove_location(uri: str) -> None:
    _write([loc for loc in load_locations() if loc.uri != uri])
