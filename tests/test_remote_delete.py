"""Network deletion is explicit and never pulls data into the local trash."""

import errno
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from commandertui import bookmarks, trash, sync
from commandertui.app import CommanderApp
from commandertui.models import OpKind, OpSide, OperationQueue, QueuedOp
from commandertui.modals import ConfirmScreen, MessageScreen
from commandertui.progress_screen import ProgressScreen


def mounts_file(tmp_path, monkeypatch, entries):
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/root / ext4 rw 0 0\n"
        + "".join(
            f"{source} {str(target).replace(' ', chr(92) + '040')} {fs} rw 0 0\n"
            for source, target, fs in entries
        )
    )
    monkeypatch.setattr(bookmarks, "PROC_MOUNTS", str(mounts))
    return mounts


@pytest.mark.parametrize(
    "fs,remote",
    [
        ("fuse.gvfsd-fuse", True),
        ("cifs", True),
        ("smb3", True),
        ("nfs", True),
        ("nfs4", True),
        ("fuse.rclone", True),
        ("fuse.sshfs", True),
        ("9p", True),
        ("davfs", True),
        ("unheard-of-network-fs", True),
        ("fuseblk", False),
        ("ext4", False),
        ("btrfs", False),
        ("apfs", False),
        ("tmpfs", False),
        ("overlay", True),
    ],
)
def test_remote_detector_uses_local_allowlist(tmp_path, monkeypatch, fs, remote):
    mount = tmp_path / "mounted disk"
    mounts_file(tmp_path, monkeypatch, [("storage", mount, fs)])
    assert trash.is_remote_path(str(mount / "document")) is remote
    assert not trash.is_remote_path(str(tmp_path / "other"))


def test_remote_detector_block_source_and_most_specific_mount(tmp_path, monkeypatch):
    mount = tmp_path / "remote"
    mounts_file(
        tmp_path,
        monkeypatch,
        [
            ("storage", mount, "cifs"),
            ("/dev/sda2", mount / "disk", "future-local-fs"),
            ("/dev/fuse", mount / "fuse", "fuse.new-network-driver"),
        ],
    )
    assert trash.is_remote_path(str(mount / "file"))
    assert not trash.is_remote_path(str(mount / "disk" / "file"))
    assert trash.is_remote_path(str(mount / "fuse" / "file"))
    assert not trash.is_remote_path(str(tmp_path / "remote-other" / "file"))


@pytest.mark.parametrize(
    "fs,remote",
    [
        ("apfs", False),
        ("hfs", False),
        ("smbfs", True),
        ("webdav", True),
        ("macfuse", True),
        ("osxfuse", True),
        ("future-fs", True),
    ],
)
def test_remote_detector_macos(tmp_path, monkeypatch, fs, remote):
    monkeypatch.setattr(trash.sys, "platform", "darwin")
    monkeypatch.setattr(
        trash.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            stdout=f"/dev/disk1 on / (apfs, local)\nserver on /Volumes/Test Disk ({fs}, nodev)\n"
        ),
    )
    assert trash.is_remote_path("/Volumes/Test Disk/file") is remote


def test_unknown_mount_table_is_not_assumed_local(tmp_path, monkeypatch):
    monkeypatch.setattr(bookmarks, "PROC_MOUNTS", str(tmp_path / "missing"))
    assert trash.is_remote_path(str(tmp_path / "file"))


def test_remote_path_never_attempts_any_trash_backend(tmp_path, monkeypatch):
    file = tmp_path / "remote" / "file"
    file.parent.mkdir()
    file.write_text("kept")
    mounts_file(tmp_path, monkeypatch, [("server", file.parent, "cifs")])
    backend = Mock()
    monkeypatch.setitem(sys.modules, "send2trash", SimpleNamespace(send2trash=backend))
    gio = Mock()
    monkeypatch.setattr(trash.subprocess, "run", gio)
    rename = Mock()
    monkeypatch.setattr(trash.os, "rename", rename)
    assert not trash.send_to_trash(str(file))
    backend.assert_not_called()
    gio.assert_not_called()
    rename.assert_not_called()
    assert file.read_text() == "kept"


def test_local_trash_fallback_exdev_never_copies(tmp_path, monkeypatch):
    file = tmp_path / "file"
    file.write_text("kept")
    mounts_file(tmp_path, monkeypatch, [])
    monkeypatch.setitem(sys.modules, "send2trash", None)
    monkeypatch.setattr(trash.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1))
    monkeypatch.setattr(trash, "_fallback_trash_dir", lambda: str(tmp_path / "trash"))
    rename = Mock(side_effect=OSError(errno.EXDEV, "cross device"))
    monkeypatch.setattr(trash.os, "rename", rename)
    move = Mock(side_effect=AssertionError("must not copy"))
    monkeypatch.setattr(trash.shutil, "move", move)
    assert not trash.send_to_trash(str(file))
    rename.assert_called_once()
    move.assert_not_called()
    assert file.read_text() == "kept"


def test_permanent_delete_does_not_follow_directory_symlinks(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    target = tmp_path / "target"
    remote.mkdir()
    target.mkdir()
    (target / "file").write_text("kept")
    link = remote / "link"
    link.symlink_to(target, target_is_directory=True)
    mounts_file(tmp_path, monkeypatch, [("server", remote, "cifs")])
    assert not sync.delete_item(str(link)).ok  # no authorization
    assert sync.delete_item(str(link), permanent=True).ok
    assert not link.is_symlink()
    assert (target / "file").read_text() == "kept"


@pytest.mark.parametrize("verify", [True, False])
def test_move_remote_source_removes_only_after_verification(tmp_path, monkeypatch, verify):
    remote = tmp_path / "remote"
    remote.mkdir()
    src = remote / "file"
    src.write_text("copied data")
    dst = tmp_path / "destination"
    mounts_file(tmp_path, monkeypatch, [("server", remote, "cifs")])
    monkeypatch.setattr(sync.os, "rename", Mock(side_effect=OSError(errno.EXDEV, "cross device")))
    backend = Mock(side_effect=AssertionError("remote source must not enter trash"))
    monkeypatch.setattr(sync, "send_to_trash", backend)
    if not verify:
        monkeypatch.setattr(sync, "verify_copy", lambda *a: False)
    result = sync.move_item(str(src), str(dst))
    assert result.ok is verify
    assert src.exists() is not verify
    assert dst.read_text() == "copied data"
    backend.assert_not_called()


def test_replace_remote_destination_never_trashes(tmp_path, monkeypatch):
    from commandertui import rclone_bridge
    from commandertui.conflicts import Resolution, detect_conflicts
    from commandertui.transfer import ConflictPolicy, build_transfer_plan, execute_transfer_plan

    src = tmp_path / "src"
    dst = tmp_path / "remote"
    src.mkdir()
    dst.mkdir()
    (src / "item").write_text("replacement")
    (dst / "item").mkdir()
    (dst / "item" / "old").write_text("old")
    mounts_file(tmp_path, monkeypatch, [("server", dst, "cifs")])
    monkeypatch.setattr(rclone_bridge, "detect_rclone_mounts", lambda: [])
    backend = Mock(side_effect=AssertionError("remote replacement must not enter trash"))
    monkeypatch.setattr(sync, "send_to_trash", backend)
    sources = [str(src / "item")]
    plan = build_transfer_plan(
        sources,
        str(dst),
        OpKind.COPY,
        detect_conflicts(sources, str(dst)),
        ConflictPolicy(Resolution.OVERWRITE, {}),
    )
    report = execute_transfer_plan(plan, OpKind.COPY)
    assert report.ok
    assert (dst / "item").read_text() == "replacement"
    backend.assert_not_called()


def test_extract_replace_remote_destination_never_trashes(tmp_path, monkeypatch):
    import zipfile
    from commandertui import archive

    dst = tmp_path / "remote"
    dst.mkdir()
    (dst / "file").write_text("old")
    zipped = tmp_path / "test.zip"
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.writestr("file", "new")
    mounts_file(tmp_path, monkeypatch, [("server", dst, "cifs")])
    backend = Mock(side_effect=AssertionError("remote replacement must not enter trash"))
    monkeypatch.setattr(archive, "send_to_trash", backend)
    assert archive.extract_archive(str(zipped), str(dst), conflict_policy="replace").ok
    assert (dst / "file").read_text() == "new"
    backend.assert_not_called()


async def main_ready(pilot):
    for _ in range(5):
        await pilot.pause()
        if pilot.app.screen.__class__.__name__ != "BootScreen":
            break
        await pilot.press("enter")


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", [False, True])
async def test_f8_remote_warns_and_defaults_to_cancel(tmp_path, monkeypatch, confirm):
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    remote.mkdir()
    local.mkdir()
    file = remote / "file"
    file.write_text("data")
    mounts_file(tmp_path, monkeypatch, [("server", remote, "fuse.gvfsd-fuse")])
    backend = Mock(side_effect=AssertionError("remote DELETE must not enter trash"))
    monkeypatch.setattr(sync, "send_to_trash", backend)
    app = CommanderApp(left_path=str(remote), right_path=str(local))
    async with app.run_test(size=(120, 40)) as pilot:
        await main_ready(pilot)
        await pilot.press("down", "f8")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.focused.id == "no"
        assert "network location" in app.screen.title_text
        assert "permanently" in app.screen.title_text
        assert "Delete permanently" in str(app.screen.query_one("#yes").label)
        await pilot.click("#yes" if confirm else "#no")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.screen_stack[-1].__class__.__name__ == "ProgressScreen"
    assert file.exists() is not confirm
    backend.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_delete_uses_local_trash_and_remote_permanent_removal(tmp_path, monkeypatch):
    root = tmp_path / "items"
    root.mkdir()
    local = root / "local"
    remote = root / "remote"
    local.write_text("local")
    remote.mkdir()
    (remote / "file").write_text("remote")
    mounts_file(tmp_path, monkeypatch, [("server", remote, "cifs")])
    trashed = []
    monkeypatch.setattr(sync, "send_to_trash", lambda p: trashed.append(p) or True)
    app = CommanderApp(left_path=str(root), right_path=str(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await main_ready(pilot)
        app._panel("left").marked = {str(local), str(remote)}
        await pilot.press("f8")
        await pilot.pause()
        assert app.screen.focused.id == "no"
        await pilot.click("#yes")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert trashed == [str(local)]
    assert not remote.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_queue_progress_closes_when_cancelled_worker_finishes_or_errors(tmp_path, monkeypatch, raises):
    import threading
    from commandertui import run_queue
    from commandertui.executor import ExecutionReport

    started = threading.Event()

    def execute(*args, cancel_check, **kwargs):
        started.set()
        # Bounded wait, no filesystem operation at all.
        for _ in range(1000):
            if cancel_check():
                if raises:
                    raise OSError("simulated network error after cancel")
                return ExecutionReport(cancelled=True)
            threading.Event().wait(0.005)
        raise AssertionError("cancel was not delivered")

    monkeypatch.setattr(run_queue, "execute", execute)
    root = tmp_path / "root"
    root.mkdir()
    app = CommanderApp(left_path=str(root), right_path=str(root))
    async with app.run_test(size=(120, 40)) as pilot:
        await main_ready(pilot)
        queue = OperationQueue([QueuedOp(OpKind.DELETE, OpSide.LEFT, "file", False)])
        run_queue.run_queue_with_progress(app.screen, "Delete", queue, str(root), str(root))
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()
        assert started.is_set()
        assert isinstance(app.screen, ProgressScreen)
        await pilot.click("#cancel")
        await pilot.click("#yes-cancel")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.query(ProgressScreen)
        assert isinstance(app.screen, MessageScreen)


@pytest.mark.asyncio
async def test_compare_mirror_warns_before_remote_delete(tmp_path, monkeypatch):
    left = tmp_path / "left"
    right = tmp_path / "remote"
    left.mkdir()
    right.mkdir()
    extra = right / "extra"
    extra.write_text("keep on cancel")
    mounts_file(tmp_path, monkeypatch, [("server", right, "cifs")])
    app = CommanderApp(left_path=str(left), right_path=str(right))
    async with app.run_test(size=(120, 40)) as pilot:
        await main_ready(pilot)
        await pilot.press("c")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.press("m")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert "network location" in app.screen.title_text
        assert "permanently" in app.screen.title_text
        assert app.screen.focused.id == "no"
        await pilot.press("enter")
        await pilot.pause()
    assert extra.read_text() == "keep on cancel"


@pytest.mark.asyncio
async def test_remote_replace_dialog_explains_permanent_replacement(tmp_path, monkeypatch):
    from commandertui.modals import ConflictScreen

    src = tmp_path / "src"
    dst = tmp_path / "remote"
    src.mkdir()
    dst.mkdir()
    (src / "file").write_text("new")
    (dst / "file").write_text("old")
    mounts_file(tmp_path, monkeypatch, [("server", dst, "cifs")])
    app = CommanderApp(left_path=str(src), right_path=str(dst))
    async with app.run_test(size=(120, 40)) as pilot:
        await main_ready(pilot)
        await pilot.press("down", "f5")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, ConflictScreen)
        assert "permanent on network location" in str(app.screen.query_one("#conflict-overwrite").label)
        await pilot.press("escape")
        await pilot.pause()
    assert (dst / "file").read_text() == "old"


def test_detector_classifies_symlinks_by_the_link_location(tmp_path, monkeypatch):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    (remote / "target").write_text("keep")
    (local / "link").symlink_to(remote / "target")
    (local / "remote-parent").symlink_to(remote, target_is_directory=True)
    mounts_file(tmp_path, monkeypatch, [("server", remote, "cifs")])
    assert not trash.is_remote_path(str(local / "link"))
    assert trash.is_remote_path(str(local / "remote-parent" / "target"))


@pytest.mark.parametrize("operation", ["create", "extract"])
def test_archive_replacement_dialog_warns_and_cancel_preserves_data(tmp_path, monkeypatch, operation):
    import zipfile
    from commandertui import archive

    src = tmp_path / "src"
    dst = tmp_path / "remote"
    src.mkdir()
    dst.mkdir()
    source = src / "file"
    source.write_text("data")
    existing = dst / ("file.tar.gz" if operation == "create" else "file")
    existing.write_text("keep until confirmed")
    mounts_file(tmp_path, monkeypatch, [("server", dst, "cifs")])
    push = Mock()
    owner = SimpleNamespace(app=SimpleNamespace(push_screen=push))
    left = SimpleNamespace(path=str(src))
    right = SimpleNamespace(path=str(dst))
    if operation == "create":
        archive.run_create_archive(owner, left, right, [str(source)])
        _, callback = push.call_args.args
        callback(str(existing))
    else:
        zipped = src / "test.zip"
        with zipfile.ZipFile(zipped, "w") as zf:
            zf.writestr("file", "replacement")
        archive.run_extract_archive(owner, left, right, str(zipped))
        screen, callback = push.call_args.args
        callback(screen.options[0])
    screen, callback = push.call_args.args
    assert "permanent on network location" in screen.options[0]
    assert screen.options[screen.initial] == "Cancel"
    callback("Cancel")
    assert existing.read_text() == "keep until confirmed"
