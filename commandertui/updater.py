"""Update check + self-update. Self-contained on purpose: copy this file into
another app and change PACKAGE / REPO to give it the same behavior.

What it does, and nothing more:
  * on every launch, in the background, asks PyPI for the latest version (a
    plain GET; nothing about the user is sent);
  * tells the app if a newer one exists and it has not been skipped;
  * builds the right upgrade command for however this copy was installed.
It never installs anything by itself -- the app runs the command only after
the user says yes, with the real terminal attached so any password prompt
(sudo, for the Arch package) is typed by the user, never read here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass

from . import __version__
from .bookmarks import CONFIG_DIR

PACKAGE = "commandertui"
REPO = "juniorbarchini-oss/commandertui"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CACHE_FILE = os.path.join(CONFIG_DIR, "update.json")
FETCH_TIMEOUT = 4
ENV_DISABLE = "COMMANDERTUI_NO_UPDATE_CHECK"
ENV_FAKE_LATEST = "COMMANDERTUI_FAKE_LATEST"  # shows the update dialog without a real release


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str
    install: str  # "pacman" | "pipx" | "pip" | "source"


def version_key(version: str) -> tuple[tuple[int, ...], int]:
    """Orderable key: 1.10.0 > 1.9.0, and 1.2.0 > 1.2.0rc1 (a pre-release sorts lower)."""
    match = re.match(r"\d+(\.\d+)*", version.strip().lstrip("v"))
    if not match:
        return ((0,), 0)
    numbers = tuple(int(n) for n in match.group(0).split("."))
    return (numbers, 1 if match.end() == len(version.strip().lstrip("v")) else 0)


def is_newer(latest: str, current: str) -> bool:
    return version_key(latest) > version_key(current)


def _load_cache() -> dict:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except (OSError, ValueError):
        pass  # a read-only home must never break the app


def fetch_latest() -> str | None:
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=FETCH_TIMEOUT) as response:
            return json.load(response)["info"]["version"]
    except (OSError, ValueError, KeyError):
        return None  # offline, PyPI down, odd answer: stay silent


def detect_install() -> str:
    """How this copy got here: decides which command can upgrade it."""
    package_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(os.path.dirname(package_dir), ".git")):
        return "source"
    if "/pipx/venvs/" in sys.prefix.replace("\\", "/"):
        return "pipx"
    try:
        owned = subprocess.run(["pacman", "-Qo", package_dir], capture_output=True, timeout=10)
        if owned.returncode == 0:
            return "pacman"
    except (OSError, subprocess.SubprocessError):
        pass
    return "pip"


# Arch: the PKGBUILD in the repo is only updated after a release is tagged
# (its checksum can't exist before the tarball does), so take the one from the
# release's own tag, point it at that version and let updpkgsums fill the
# checksum from the real tarball, then build and install.
_ARCH_SCRIPT = """set -e
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
curl -fsSL https://raw.githubusercontent.com/{repo}/v{latest}/packaging/arch/PKGBUILD -o PKGBUILD
sed -i 's/^pkgver=.*/pkgver={latest}/; s/^pkgrel=.*/pkgrel=1/' PKGBUILD
updpkgsums
makepkg -si
"""


def update_command(install: str, latest: str) -> list[str] | None:
    """The command to run for `install`, or None when there isn't a safe one."""
    if install == "pipx":
        return ["pipx", "upgrade", PACKAGE]
    if install == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]
    if install == "pacman":
        return ["bash", "-c", _ARCH_SCRIPT.format(repo=REPO, latest=latest)]
    return None  # git checkout: the user updates it with git


def updates_disabled(cli_flag: bool = False) -> bool:
    return cli_flag or bool(os.environ.get(ENV_DISABLE))


def check_status(current: str = __version__, now: float | None = None) -> tuple[UpdateInfo | None, str]:
    """Blocking (network): call from a worker thread. Returns (info, status)
    with status "new" (info set), "ok" (up to date), "skipped" (a newer one
    exists but the user skipped it) or "offline"."""
    now = time.time() if now is None else now
    cache = _load_cache()

    # Asked every launch (one small GET in the background): a release is seen
    # the moment it exists, not up to a day later.
    latest = os.environ.get(ENV_FAKE_LATEST) or fetch_latest()
    if latest is None:
        return None, "offline"
    cache.update(checked=now, latest=latest)
    _save_cache(cache)

    if not is_newer(latest, current):
        return None, "ok"
    if cache.get("skipped") == latest:
        return None, "skipped"
    return UpdateInfo(current=current, latest=latest, install=detect_install()), "new"


def check(current: str = __version__, now: float | None = None) -> UpdateInfo | None:
    """Just the UpdateInfo of check_status(): None when there is nothing to offer."""
    return check_status(current, now)[0]


def check_now(current: str = __version__) -> tuple[UpdateInfo | None, bool]:
    """The manual "Check for updates": always asks PyPI (ignores the daily
    cache and a previous Skip, since the user is asking). Blocking (network).
    Returns (UpdateInfo or None, reachable)."""
    latest = os.environ.get(ENV_FAKE_LATEST) or fetch_latest()
    if latest is None:
        return None, False
    cache = _load_cache()
    cache.update(checked=time.time(), latest=latest)
    cache.pop("skipped", None)
    _save_cache(cache)
    if not is_newer(latest, current):
        return None, True
    return UpdateInfo(current=current, latest=latest, install=detect_install()), True


def skip_version(version: str) -> None:
    cache = _load_cache()
    cache["skipped"] = version
    _save_cache(cache)


def run_update(info: UpdateInfo) -> bool:
    """Run the upgrade attached to the real terminal. True if it exited 0."""
    command = update_command(info.install, info.latest)
    if command is None:
        return False
    return subprocess.run(command).returncode == 0
