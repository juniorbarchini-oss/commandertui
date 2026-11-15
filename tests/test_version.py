"""The version is written in four files; they must never drift apart."""

import importlib.util
import os

from commandertui import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bump_module():
    spec = importlib.util.spec_from_file_location(
        "bump_version", os.path.join(ROOT, "packaging", "bump_version.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_file_carries_the_package_version():
    versions = _bump_module().read_versions()
    assert versions == {rel: __version__ for rel in versions}, (
        "run: python packaging/bump_version.py " + __version__
    )


def test_bump_rewrites_all_files(tmp_path, monkeypatch):
    bump = _bump_module()
    for rel in bump.TARGETS:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            target.write_text(fh.read(), encoding="utf-8")
    monkeypatch.setattr(bump, "ROOT", str(tmp_path))
    assert bump.main(["9.8.7"]) == 0
    assert set(bump.read_versions().values()) == {"9.8.7"}
    assert bump.main(["--check"]) == 0
    assert bump.main(["not-a-version"]) == 2
    (tmp_path / "packaging/arch/PKGBUILD").write_text("pkgver=1.0.0\n", encoding="utf-8")
    assert bump.main(["--check"]) == 1
