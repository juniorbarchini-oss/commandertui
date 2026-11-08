import os
import shutil

import pytest

from commandertui import sync
from commandertui.conflicts import Resolution, detect_conflicts
from commandertui.models import OpKind
from commandertui.transfer import ConflictPolicy, build_transfer_plan, execute_transfer_plan


def write(path, text="new", mtime=200):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    src, dst, trash = (tmp_path / name for name in ("src", "dst", "trash"))
    src.mkdir()
    dst.mkdir()
    trash.mkdir()
    calls = []

    def trash_item(path):
        calls.append(path)
        shutil.move(path, trash / str(len(calls)))
        return True

    monkeypatch.setattr(sync, "send_to_trash", trash_item)
    return src, dst, calls


def plan_for(src, dst, resolution=Resolution.OVERWRITE, kind=OpKind.COPY, overrides=None):
    sources = [str(src)]
    return build_transfer_plan(
        sources,
        str(dst),
        kind,
        detect_conflicts(sources, str(dst)),
        ConflictPolicy(resolution, overrides or {}),
    )


@pytest.mark.parametrize(
    "resolution,expected",
    [
        (Resolution.OVERWRITE, "new"),
        (Resolution.SKIP, "old"),
        (Resolution.ONLY_NEWER, "new"),
        (Resolution.RENAME, "old"),
    ],
)
def test_file_policies(roots, resolution, expected):
    src, dst, calls = roots
    source = write(src / "file.txt")
    target = write(dst / source.name, "old", 100)
    plan = plan_for(source, dst, resolution)
    report = execute_transfer_plan(plan, OpKind.COPY)
    assert report.ok
    assert target.read_text() == expected
    assert source.read_text() == "new"
    if resolution == Resolution.RENAME:
        assert (dst / "file_1.txt").read_text() == "new"
    assert report.copied == (0 if resolution == Resolution.SKIP else 1)
    assert calls == []


def test_rename_chain(roots):
    src, dst, _ = roots
    source = write(src / "file.txt")
    write(dst / source.name, "old", 100)
    write(dst / "file_1.txt", "reserved")
    plan = plan_for(source, dst, Resolution.RENAME)
    assert plan.items[0].dst_abs == str(dst / "file_2.txt")
    assert execute_transfer_plan(plan, OpKind.COPY).ok
    assert (dst / "file_1.txt").read_text() == "reserved"


def test_only_newer_skips_older(roots):
    src, dst, _ = roots
    source = write(src / "file", mtime=100)
    write(dst / source.name, "old", 200)
    report = execute_transfer_plan(plan_for(source, dst, Resolution.ONLY_NEWER), OpKind.COPY)
    assert report.ok and report.skipped == [str(source)]
    assert (dst / "file").read_text() == "old"


def test_merge_directory(roots):
    src, dst, _ = roots
    folder = src / "folder"
    write(folder / "missing" / "new")
    write(folder / "keep")
    write(folder / "replace")
    write(dst / "folder" / "keep", "old", 100)
    write(dst / "folder" / "replace", "old", 100)
    write(dst / "folder" / "extra", "extra")
    plan = plan_for(
        folder,
        dst,
        Resolution.SKIP,
        overrides={"folder": Resolution.MERGE, "folder/replace": Resolution.OVERWRITE},
    )
    report = execute_transfer_plan(plan, OpKind.COPY)
    assert report.ok and report.copied == 2
    assert (dst / "folder" / "missing" / "new").read_text() == "new"
    assert (dst / "folder" / "keep").read_text() == "old"
    assert (dst / "folder" / "replace").read_text() == "new"
    assert (dst / "folder" / "extra").read_text() == "extra"


@pytest.mark.parametrize("kind", [OpKind.COPY, OpKind.MOVE])
def test_directory_rename_is_whole(roots, kind):
    src, dst, _ = roots
    folder = src / "folder"
    write(folder / "file")
    write(dst / "folder" / "extra", "extra")
    plan = plan_for(folder, dst, Resolution.RENAME, kind)
    assert len(plan.items) == 1 and plan.items[0].is_dir_whole
    assert execute_transfer_plan(plan, kind).ok
    assert (dst / "folder_1" / "file").read_text() == "new"
    assert (dst / "folder" / "extra").read_text() == "extra"
    assert folder.exists() == (kind == OpKind.COPY)


def test_move_skip_retains_source(roots):
    src, dst, calls = roots
    source = write(src / "file")
    write(dst / "file", "old", 100)
    report = execute_transfer_plan(plan_for(source, dst, Resolution.SKIP, OpKind.MOVE), OpKind.MOVE)
    assert report.ok and report.skipped == [str(source)]
    assert source.read_text() == "new" and calls == []


def test_move_directory_with_skipped_file(roots):
    src, dst, calls = roots
    folder = src / "folder"
    retained = write(folder / "keep")
    moved = write(folder / "nested" / "move")
    write(dst / "folder" / "keep", "old", 100)
    plan = plan_for(folder, dst, kind=OpKind.MOVE, overrides={"folder/keep": Resolution.SKIP})
    report = execute_transfer_plan(plan, OpKind.MOVE)
    assert report.ok and report.copied == 1
    assert retained.read_text() == "new"
    assert not moved.exists() and folder.exists()
    assert calls == [str(moved)]
    assert (dst / "folder" / "nested" / "move").read_text() == "new"


def test_move_cancellation_mid_file(roots, monkeypatch):
    src, dst, calls = roots
    folder = src / "folder"
    first = write(folder / "a", "first")
    second = write(folder / "b", "second" * 20)
    monkeypatch.setattr(sync, "CHUNK_SIZE", 4)
    cancelled = False

    def progress(done, total, name):
        nonlocal cancelled
        if name == "b" and done > 0:
            cancelled = True

    report = execute_transfer_plan(
        plan_for(folder, dst, kind=OpKind.MOVE), OpKind.MOVE, progress, lambda: cancelled
    )
    assert report.cancelled and not report.ok
    assert not first.exists()
    assert (dst / "folder" / "a").read_text() == "first"
    assert second.read_text() == "second" * 20
    assert not (dst / "folder" / "b").exists()
    assert calls == [str(first)]
    assert not list(dst.rglob("*.tmp"))


@pytest.mark.parametrize("resolution", [Resolution.MERGE, Resolution.ONLY_NEWER])
def test_move_rejects_disallowed_policies(roots, resolution):
    src, dst, _ = roots
    source = write(src / "file")
    with pytest.raises(ValueError):
        plan_for(source, dst, resolution, OpKind.MOVE)
    with pytest.raises(ValueError):
        plan_for(source, dst, kind=OpKind.MOVE, overrides={"file": resolution})


def test_overwrite_type_mismatch_trashes_destination(roots):
    src, dst, calls = roots
    source = write(src / "file")
    old = write(dst / "file" / "old", "old")
    report = execute_transfer_plan(plan_for(source, dst), OpKind.COPY)
    assert report.ok and calls == [str(old.parent)]
    assert (dst / "file").read_text() == "new"


def test_readonly_destination_retains_move_source(roots):
    src, dst, calls = roots
    source = write(src / "file")
    plan = plan_for(source, dst, kind=OpKind.MOVE)
    dst.chmod(0o555)
    try:
        report = execute_transfer_plan(plan, OpKind.MOVE)
    finally:
        dst.chmod(0o755)
    assert not report.ok and report.failed and not report.cancelled
    assert source.read_text() == "new" and calls == []


def test_real_tree_twenty_files(roots):
    src, dst, _ = roots
    folder = src / "tree"
    for index in range(20):
        rel = f"level1/level2/level3/file{index}.txt"
        write(folder / rel, f"source-{index}")
        if index % 2 == 0:
            write(dst / "tree" / rel, f"old-{index}", 100)
    plan = plan_for(folder, dst, overrides={"tree": Resolution.MERGE})
    report = execute_transfer_plan(plan, OpKind.COPY)
    assert report.ok and report.copied == 20
    for index in range(20):
        assert (
            dst / "tree" / "level1" / "level2" / "level3" / f"file{index}.txt"
        ).read_text() == f"source-{index}"


def test_empty_directory_whole_and_move_cleanup(roots):
    src, dst, _ = roots
    folder = src / "empty"
    folder.mkdir()
    plan = plan_for(folder, dst, kind=OpKind.MOVE)
    assert len(plan.items) == 1 and plan.items[0].is_dir_whole
    assert execute_transfer_plan(plan, OpKind.MOVE).ok
    assert not folder.exists() and (dst / "empty").is_dir()


def test_failed_verification_retains_source(roots, monkeypatch):
    src, dst, calls = roots
    source = write(src / "file")
    monkeypatch.setattr(sync, "verify_copy", lambda *args: False)
    report = execute_transfer_plan(plan_for(source, dst, kind=OpKind.MOVE), OpKind.MOVE)
    assert not report.ok and report.failed
    assert source.read_text() == "new" and calls == []


def test_destination_race_does_not_overwrite(roots):
    src, dst, _ = roots
    source = write(src / "file")
    plan = plan_for(source, dst)
    write(dst / "file", "appeared")
    report = execute_transfer_plan(plan, OpKind.COPY)
    assert not report.ok
    assert (dst / "file").read_text() == "appeared"


def test_skipped_empty_directory_is_retained(roots):
    src, dst, calls = roots
    folder = src / "folder"
    (folder / "empty").mkdir(parents=True)
    write(dst / "folder" / "empty", "old", 100)
    plan = plan_for(folder, dst, kind=OpKind.MOVE, overrides={"folder/empty": Resolution.SKIP})
    report = execute_transfer_plan(plan, OpKind.MOVE)
    assert report.ok and (folder / "empty").is_dir() and calls == []


def test_pre_cancelled_move_preserves_empty_directories(roots):
    src, dst, calls = roots
    folder = src / "folder"
    (folder / "empty").mkdir(parents=True)
    (dst / "folder").mkdir()
    plan = plan_for(folder, dst, kind=OpKind.MOVE)
    report = execute_transfer_plan(plan, OpKind.MOVE, cancel_check=lambda: True)
    assert report.cancelled and (folder / "empty").is_dir() and calls == []


def test_trash_failure_does_not_replace_destination(roots, monkeypatch):
    src, dst, _ = roots
    source = write(src / "file")
    write(dst / "file" / "old", "old")
    monkeypatch.setattr(sync, "send_to_trash", lambda path: False)
    report = execute_transfer_plan(plan_for(source, dst, kind=OpKind.MOVE), OpKind.MOVE)
    assert not report.ok and source.read_text() == "new"
    assert (dst / "file" / "old").read_text() == "old"


def test_duplicate_folder_renames_reserve_names(roots):
    src, dst, _ = roots
    first = src / "one" / "folder"
    second = src / "two" / "folder"
    write(first / "file")
    write(second / "file")
    (dst / "folder").mkdir()
    sources = [str(first), str(second)]
    plan = build_transfer_plan(
        sources, str(dst), OpKind.COPY, detect_conflicts(sources, str(dst)), ConflictPolicy(Resolution.RENAME)
    )
    assert [item.dst_abs for item in plan.items] == [str(dst / "folder_1"), str(dst / "folder_2")]
    assert execute_transfer_plan(plan, OpKind.COPY).ok


def test_on_item_and_progress(roots):
    src, dst, _ = roots
    source = write(src / "file")
    notifications, progress = [], []
    plan = plan_for(source, dst)
    report = execute_transfer_plan(
        plan,
        OpKind.COPY,
        progress=lambda *args: progress.append(args),
        on_item=lambda item, result: notifications.append((item, result.copied)),
    )
    assert report.ok and progress[-1][0] == 3
    assert notifications == [(plan.items[0], 1)]


def test_destination_inside_source_rejected(roots):
    src, _, _ = roots
    write(src / "file")
    with pytest.raises(ValueError):
        plan_for(src, src / "nested")


@pytest.fixture
def real_roots(tmp_path):
    # Deliberately no send_to_trash patch: exercise the native trash path.
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    return src, dst


@pytest.mark.parametrize("kind", [OpKind.COPY, OpKind.MOVE])
@pytest.mark.parametrize("link_kind", ["relative", "absolute", "broken"])
def test_real_symlinks(real_roots, kind, link_kind):
    src, dst = real_roots
    target = write(src / "target", "target-content")
    link = src / "link"
    text = str(target) if link_kind == "absolute" else "missing" if link_kind == "broken" else "target"
    link.symlink_to(text)
    report = execute_transfer_plan(plan_for(link, dst, kind=kind), kind)
    assert report.ok and report.copied == 1
    assert (dst / "link").is_symlink()
    assert os.readlink(dst / "link") == text
    assert os.path.lexists(link) == (kind == OpKind.COPY)
    assert target.read_text() == "target-content"


@pytest.mark.parametrize("kind", [OpKind.COPY, OpKind.MOVE])
def test_real_identical_kept(real_roots, kind):
    src, dst = real_roots
    source = write(src / "file", "same")
    write(dst / "file", "same")
    plan = plan_for(source, dst, kind=kind)
    assert plan.identical_kept == 1
    assert plan.identical_kept_paths == [str(source)]
    report = execute_transfer_plan(plan, kind)
    assert report.ok and report.copied == 0
    assert report.identical_kept == 1
    assert report.identical_kept_paths == [str(source)]
    assert source.read_text() == "same"


@pytest.mark.parametrize("kind", [OpKind.COPY, OpKind.MOVE])
def test_nested_identical_kept(real_roots, kind):
    src, dst = real_roots
    source = write(src / "folder" / "nested" / "file", "same")
    write(dst / "folder" / "nested" / "file", "same")
    plan = plan_for(src / "folder", dst, kind=kind)
    assert plan.identical_kept_paths == [str(source)]
    report = execute_transfer_plan(plan, kind)
    assert report.ok and report.identical_kept == 1
    assert report.identical_kept_paths == [str(source)]
    assert source.read_text() == "same"


@pytest.mark.parametrize("resolution", [Resolution.RENAME, Resolution.SKIP, Resolution.OVERWRITE])
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("kind", [OpKind.COPY, OpKind.MOVE])
def test_duplicate_sources(roots, resolution, override, kind):
    src, dst, _ = roots
    sources = [write(src / str(index) / "file.txt", f"source-{index}") for index in range(3)]
    paths = [str(source) for source in sources]
    detection = detect_conflicts(paths, str(dst))
    assert detection.duplicate_sources == ["file.txt"] and detection.has_conflicts
    policy = (
        ConflictPolicy(Resolution.OVERWRITE, {"file.txt": resolution})
        if override
        else ConflictPolicy(resolution)
    )
    plan = build_transfer_plan(paths, str(dst), kind, detection, policy)
    report = execute_transfer_plan(plan, kind)
    assert report.ok
    if resolution == Resolution.RENAME:
        assert report.copied == 3
        assert [(dst / name).read_text() for name in ("file.txt", "file_1.txt", "file_2.txt")] == [
            "source-0",
            "source-1",
            "source-2",
        ]
        retained = []
    else:
        winner = 2 if resolution == Resolution.OVERWRITE else 0
        assert (dst / "file.txt").read_text() == f"source-{winner}"
        assert report.copied == 1 and len(report.skipped) == 2
        retained = [index for index in range(3) if index != winner]
    for index, source in enumerate(sources):
        assert source.exists() == (kind == OpKind.COPY or index in retained)


def test_duplicate_conflicts_keep_source_identity(roots):
    src, dst, _ = roots
    older = write(src / "a" / "file", "old", 100)
    newer = write(src / "b" / "file", "new", 300)
    write(dst / "file", "dst", 200)
    sources = [str(newer), str(older)]
    detection = detect_conflicts(sources, str(dst))
    assert len(detection.conflicts) == 2
    assert set(detection.conflicts_by_source) == set(sources)
    # ONLY_NEWER must use the conflict belonging to each source, not whichever
    # homonymous entry happened to overwrite a rel_path dictionary key.
    plan = build_transfer_plan(
        sources, str(dst), OpKind.COPY, detection, ConflictPolicy(Resolution.ONLY_NEWER)
    )
    assert plan.items[0].action == "copy"
    assert plan.items[1].action == "skip"


def test_identical_kept_not_counted_before_cancel(real_roots):
    src, dst = real_roots
    source = write(src / "file")
    write(dst / "file")
    report = execute_transfer_plan(plan_for(source, dst), OpKind.COPY, cancel_check=lambda: True)
    assert report.cancelled and report.identical_kept == 0 and report.identical_kept_paths == []


@pytest.mark.parametrize("kind", [OpKind.COPY, OpKind.MOVE])
def test_real_overwrite_symlinks_keeps_referents(real_roots, kind):
    src, dst = real_roots
    source_target = write(src / "source-target", "source referent")
    old_target = write(dst / "old-target", "old referent")
    link = src / "link"
    link.symlink_to(source_target)
    (dst / "link").symlink_to(old_target)
    # Different link lengths/mtimes ensure a metadata conflict.
    os.utime(link, (100, 100), follow_symlinks=False)
    os.utime(dst / "link", (200, 200), follow_symlinks=False)
    report = execute_transfer_plan(plan_for(link, dst, kind=kind), kind)
    assert report.ok
    assert os.readlink(dst / "link") == str(source_target)
    assert source_target.read_text() == "source referent"
    assert old_target.read_text() == "old referent"
    assert os.path.lexists(link) == (kind == OpKind.COPY)
