import os

import pytest

from commandertui.conflicts import (
    Conflict,
    ConflictReport,
    Resolution,
    allowed_resolutions,
    detect_conflicts,
    rename_target,
    resolve,
)
from commandertui.models import EntryKind, FileEntry, OpKind


def write(path, content="abc", mtime=100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def roots(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    return src, dst


def test_single_new_file(roots):
    src, dst = roots
    source = write(src / "file")
    report = detect_conflicts([str(source)], str(dst))
    assert report == ConflictReport(total_src_files_new=1)
    assert not report.has_conflicts


def test_single_conflict(roots):
    src, dst = roots
    source = write(src / "file", "new", 200)
    write(dst / "file", "old", 100)
    report = detect_conflicts([str(source)], str(dst))
    assert report.has_conflicts
    assert report.total_src_files_new == report.identical == 0
    conflict = report.conflicts[0]
    assert (conflict.rel_path, conflict.name, conflict.newer) == ("file", "file", "src")
    assert conflict.src.kind == conflict.dst.kind == EntryKind.FILE
    assert not conflict.kind_mismatch


@pytest.mark.parametrize("delta", [0, 1.9, 2.0, -2.0])
def test_identical_and_mtime_tolerance(roots, delta):
    src, dst = roots
    source = write(src / "file")
    write(dst / "file", mtime=100 + delta)
    report = detect_conflicts([str(source)], str(dst))
    assert report.identical == 1
    assert report.has_conflicts
    assert len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert conflict.identical and conflict.newer == "same"
    assert report.conflicts_by_source[str(source)] is conflict
    assert report.identical_paths == [str(source)]


@pytest.mark.parametrize("delta,newer", [(2.1, "dst"), (-2.1, "src")])
def test_beyond_tolerance(roots, delta, newer):
    src, dst = roots
    source = write(src / "file")
    write(dst / "file", mtime=100 + delta)
    report = detect_conflicts([str(source)], str(dst))
    assert report.conflicts[0].newer == newer
    assert report.identical == 0


def test_size_difference_with_equal_mtime(roots):
    src, dst = roots
    source = write(src / "file", "longer")
    write(dst / "file")
    conflict = detect_conflicts([str(source)], str(dst)).conflicts[0]
    assert conflict.newer == "same"


def test_directory_merge_inventory(roots):
    src, dst = roots
    write(src / "folder" / "nested" / "changed", "new", 200)
    write(dst / "folder" / "nested" / "changed", "old")
    write(src / "folder" / "same")
    write(dst / "folder" / "same")
    write(src / "folder" / "missing" / "new")
    write(dst / "folder" / "destination_only")
    report = detect_conflicts([str(src / "folder")], str(dst))
    assert report.dir_collisions == ["folder"]
    assert report.identical == report.total_src_files_new == 1
    assert [c.rel_path for c in report.conflicts] == ["folder/nested/changed", "folder/same"]
    assert [c.identical for c in report.conflicts] == [False, True]


def test_new_directory_counts_files(roots):
    src, dst = roots
    write(src / "folder" / "nested" / "one")
    write(src / "folder" / "two")
    report = detect_conflicts([str(src / "folder")], str(dst))
    assert report.total_src_files_new == 2
    assert not report.has_conflicts
    assert report.dir_collisions == []


@pytest.mark.parametrize("source_is_dir", [True, False])
@pytest.mark.parametrize("nested", [True, False])
def test_kind_mismatch(roots, source_is_dir, nested):
    src, dst = roots
    relative = "folder/item" if nested else "item"
    directory = (src if source_is_dir else dst) / relative
    directory.mkdir(parents=True)
    write(directory / "child")
    write((dst if source_is_dir else src) / relative)
    source = src / ("folder" if nested else "item")
    report = detect_conflicts([str(source)], str(dst))
    assert len(report.conflicts) == 1
    assert report.conflicts[0].kind_mismatch
    assert report.conflicts[0].rel_path == relative
    assert report.total_src_files_new == report.identical == 0


@pytest.mark.parametrize("name,renamed", [("foto.jpg", "foto_1.jpg"), ("carpeta", "carpeta_1")])
def test_rename_target(tmp_path, name, renamed):
    path = tmp_path / name
    write(path)
    assert rename_target(str(path)) == str(tmp_path / renamed)
    assert sorted(p.name for p in tmp_path.iterdir()) == [name]


def test_rename_chain_and_dangling_link(tmp_path):
    write(tmp_path / "foto.jpg")
    (tmp_path / "foto_1.jpg").symlink_to(tmp_path / "missing")
    assert rename_target(str(tmp_path / "foto.jpg")) == str(tmp_path / "foto_2.jpg")


def test_allowed_resolutions():
    assert allowed_resolutions(OpKind.COPY) == list(Resolution)
    assert allowed_resolutions(OpKind.MOVE) == [
        Resolution.OVERWRITE,
        Resolution.SKIP,
        Resolution.RENAME,
        Resolution.CANCEL,
    ]
    with pytest.raises(ValueError):
        allowed_resolutions(OpKind.DELETE)


@pytest.mark.parametrize("newer,action", [("src", "copy"), ("dst", "skip"), ("same", "skip")])
def test_only_newer(newer, action):
    entry = FileEntry("file", "file", EntryKind.FILE, 3, 100)
    conflict = Conflict("file", "file", entry, entry, False, newer)
    assert resolve(conflict, Resolution.ONLY_NEWER) == action


@pytest.mark.parametrize(
    "resolution,action",
    [(Resolution.OVERWRITE, "copy"), (Resolution.SKIP, "skip"), (Resolution.RENAME, "rename")],
)
def test_resolve_is_pure(roots, resolution, action):
    src, dst = roots
    source = write(src / "file", "new")
    target = write(dst / "file", "old", 200)
    conflict = detect_conflicts([str(source)], str(dst)).conflicts[0]
    assert resolve(conflict, resolution) == action
    assert source.read_text() == "new"
    assert target.read_text() == "old"


@pytest.mark.parametrize("resolution", [Resolution.MERGE, Resolution.CANCEL])
def test_caller_level_resolutions(resolution):
    entry = FileEntry("file", "file", EntryKind.FILE, 3, 100)
    conflict = Conflict("file", "file", entry, entry, False, "same")
    with pytest.raises(ValueError):
        resolve(conflict, resolution)


def test_cancellation_discards_partial_report(roots):
    src, dst = roots
    source = write(src / "folder" / "file")
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        return calls >= 3

    assert detect_conflicts([str(source.parent)], str(dst), cancel) == ConflictReport()


def test_symlink_to_directory_is_not_traversed(roots):
    src, dst = roots
    write(src / "real" / "file")
    (src / "link").symlink_to(src / "real", target_is_directory=True)
    write(dst / "link")
    report = detect_conflicts([str(src / "link")], str(dst))
    assert report.conflicts[0].src.kind == EntryKind.SYMLINK
    assert report.conflicts[0].kind_mismatch


def test_identical_symlinks_are_conflicts(roots):
    src, dst = roots
    (src / "link").symlink_to("missing")
    (dst / "link").symlink_to("missing")
    for path in (src / "link", dst / "link"):
        os.utime(path, (100, 100), follow_symlinks=False)
    report = detect_conflicts([str(src / "link")], str(dst))
    assert report.has_conflicts and report.identical == 1
    assert report.conflicts[0].identical
    assert report.conflicts[0].src.kind == EntryKind.SYMLINK
