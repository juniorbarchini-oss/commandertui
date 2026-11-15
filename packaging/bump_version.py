#!/usr/bin/env python3
"""Set the app version everywhere it is written.

    python packaging/bump_version.py 1.8.0     # write the new version
    python packaging/bump_version.py --check   # exit 1 if the files disagree

`commandertui/__init__.py` is the source of truth (pyproject.toml reads it). The Arch
PKGBUILD, the macOS Info.plist and the README status line need a literal copy, which this
script keeps in step; tests/test_version.py fails when one of them is left behind.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_RE = r"\d+\.\d+\.\d+"

# file -> pattern with the version as the only group-less match between two captured sides
TARGETS = {
    "commandertui/__init__.py": rf'(__version__ = "){VERSION_RE}(")',
    "packaging/arch/PKGBUILD": rf"(?m)(^pkgver=){VERSION_RE}($)",
    "packaging/macos/Info.plist": (
        rf"(<key>CFBundleShortVersionString</key>\s*<string>){VERSION_RE}(</string>)"
    ),
    "README.md": rf"(?m)(^Status: \*\*v){VERSION_RE}(\*\*)",
}


def read_versions() -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for rel, pattern in TARGETS.items():
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            text = fh.read()
        match = re.search(pattern, text)
        found[rel] = text[match.end(1) : match.start(2)] if match else None
    return found


def write_version(version: str) -> None:
    for rel, pattern in TARGETS.items():
        path = os.path.join(ROOT, rel)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        new_text, count = re.subn(pattern, rf"\g<1>{version}\g<2>", text, count=1)
        if count != 1:
            raise SystemExit(f"{rel}: version line not found")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    if argv[0] == "--check":
        versions = read_versions()
        for rel, version in versions.items():
            print(f"{version or 'MISSING':10} {rel}")
        return 0 if len(set(versions.values())) == 1 and None not in versions.values() else 1
    if not re.fullmatch(VERSION_RE, argv[0]):
        print(f"Not a version like 1.8.0: {argv[0]}")
        return 2
    write_version(argv[0])
    print(f"Version set to {argv[0]} in {len(TARGETS)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
