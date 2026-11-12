"""Unit tests for the rclone Turbo Copy bridge and its integration with transfer/executor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from commandertui.conflicts import Resolution, detect_conflicts
from commandertui.diff import compare
from commandertui.executor import execute
from commandertui.models import OpKind
from commandertui.plan import plan_mirror
from commandertui.rclone_bridge import (
    RcloneMount,
    batch_transfer_files_with_rclone,
    copy_with_rclone,
    detect_rclone_mounts,
    is_rclone_transfer,
    move_with_rclone,
    resolve_rclone_endpoint,
)
from commandertui.transfer import ConflictPolicy, build_transfer_plan, execute_transfer_plan


def test_detect_rclone_mounts_parses_proc_mounts(tmp_path: Path) -> None:
    """detect_rclone_mounts extracts fuse.rclone mounts sorted longest-first."""
    fake_mounts = tmp_path / "mounts"
    fake_mounts.write_text(
        "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"
        "gdrive: /mnt/gdrive fuse.rclone rw,nosuid,nodev,relatime,user_id=1000 0 0\n"
        "gdrive:backup /mnt/gdrive/nested\\040dir fuse.rclone rw,nosuid 0 0\n"
        "/dev/sda1 /mnt/SATA_6TB ext4 rw,relatime 0 0\n",
        encoding="utf-8",
    )

    mounts = detect_rclone_mounts(proc_mounts=str(fake_mounts))

    assert len(mounts) == 2
    assert mounts[0] == RcloneMount(mountpoint="/mnt/gdrive/nested dir", remote="gdrive:backup")
    assert mounts[1] == RcloneMount(mountpoint="/mnt/gdrive", remote="gdrive:")


def test_resolve_rclone_endpoint_matches_root_and_subpaths() -> None:
    """resolve_rclone_endpoint maps FUSE paths to remote specs and keeps local paths."""
    mounts = [
        RcloneMount(mountpoint="/mnt/gdrive/sub", remote="gdrive:subroot"),
        RcloneMount(mountpoint="/mnt/gdrive", remote="gdrive:"),
    ]

    assert resolve_rclone_endpoint("/mnt/gdrive", mounts) == ("gdrive:", True)
    assert resolve_rclone_endpoint("/mnt/gdrive/backup/cines", mounts) == (
        "gdrive:backup/cines",
        True,
    )
    assert resolve_rclone_endpoint("/mnt/gdrive/sub/folder/a.txt", mounts) == (
        "gdrive:subroot/folder/a.txt",
        True,
    )
    local_spec, is_remote = resolve_rclone_endpoint("/mnt/SATA_6TB/data", mounts)
    assert is_remote is False
    assert local_spec == "/mnt/SATA_6TB/data"


def test_is_rclone_transfer_identifies_cloud_endpoints() -> None:
    """is_rclone_transfer returns True when either src or dst is on a fuse.rclone mount."""
    mounts = [RcloneMount(mountpoint="/mnt/gdrive", remote="gdrive:")]

    assert is_rclone_transfer("/mnt/gdrive/a", "/tmp/b", mounts) is True
    assert is_rclone_transfer("/tmp/a", "/mnt/gdrive/b", mounts) is True
    assert is_rclone_transfer("/tmp/a", "/tmp/b", mounts) is False


def test_copy_with_rclone_directory_and_progress(tmp_path: Path) -> None:
    """copy_with_rclone copies a directory tree using rclone and emits progress."""
    fake_cloud_backing = tmp_path / "cloud_backing"
    fake_mountpoint = tmp_path / "mnt_gdrive"
    local_dst_root = tmp_path / "local_dst"

    src_dir = fake_cloud_backing / "cines_backup"
    (src_dir / "sub").mkdir(parents=True)
    (src_dir / "index.html").write_text("<html>1</html>", encoding="utf-8")
    (src_dir / "sub" / "page.html").write_text("<html>2222</html>", encoding="utf-8")

    # Point the fake RcloneMount remote directly to fake_cloud_backing (local path acts as rclone remote)
    mounts = [RcloneMount(mountpoint=str(fake_mountpoint), remote=str(fake_cloud_backing))]

    fuse_src = fake_mountpoint / "cines_backup"
    (fuse_src / "sub").mkdir(parents=True)
    (fuse_src / "index.html").write_text("<html>1</html>", encoding="utf-8")
    (fuse_src / "sub" / "page.html").write_text("<html>2222</html>", encoding="utf-8")

    dst_dir = local_dst_root / "cines_backup"
    progress_events: list[tuple[int, int, str]] = []

    with patch("commandertui.rclone_bridge.flush_mount_cache"):
        res = copy_with_rclone(
            str(fuse_src),
            str(dst_dir),
            is_dir=True,
            progress=lambda d, t, n: progress_events.append((d, t, n)),
            mounts=mounts,
        )

    assert res.ok is True
    assert res.cancelled is False
    assert (dst_dir / "index.html").read_text(encoding="utf-8") == "<html>1</html>"
    assert (dst_dir / "sub" / "page.html").read_text(encoding="utf-8") == "<html>2222</html>"
    assert len(progress_events) > 0


def test_move_with_rclone_local_to_remote_trashes_source(tmp_path: Path) -> None:
    """Moving a local folder to an rclone mount copies via rclone and trashes the local source."""
    fake_cloud_backing = tmp_path / "cloud_backing"
    fake_cloud_backing.mkdir()
    fake_mountpoint = tmp_path / "mnt_gdrive"
    fake_mountpoint.mkdir()

    local_src = tmp_path / "to_upload"
    local_src.mkdir()
    (local_src / "doc.txt").write_text("hello cloud", encoding="utf-8")

    mounts = [RcloneMount(mountpoint=str(fake_mountpoint), remote=str(fake_cloud_backing))]
    fuse_dst = fake_mountpoint / "to_upload"

    trashed: list[str] = []

    def _fake_trash(path: str) -> bool:
        trashed.append(path)
        return True

    with (
        patch("commandertui.rclone_bridge.flush_mount_cache"),
        patch("commandertui.rclone_bridge.send_to_trash", side_effect=_fake_trash),
    ):
        res = move_with_rclone(str(local_src), str(fuse_dst), is_dir=True, mounts=mounts)

    assert res.ok is True
    assert (fake_cloud_backing / "to_upload" / "doc.txt").read_text(encoding="utf-8") == "hello cloud"
    assert trashed == [str(local_src)]


def test_batch_transfer_files_with_rclone_copies_multiple_files(tmp_path: Path) -> None:
    """batch_transfer_files_with_rclone transfers multiple relative files in a single rclone run."""
    fake_cloud_backing = tmp_path / "cloud_backing"
    fake_cloud_backing.mkdir()
    fake_mountpoint = tmp_path / "mnt_gdrive"
    fake_mountpoint.mkdir()
    local_dst = tmp_path / "dst"
    local_dst.mkdir()

    for name in ("a.txt", "b.txt", "c.txt"):
        (fake_cloud_backing / name).write_text(f"content-{name}", encoding="utf-8")
        (fake_mountpoint / name).write_text(f"content-{name}", encoding="utf-8")

    mounts = [RcloneMount(mountpoint=str(fake_mountpoint), remote=str(fake_cloud_backing))]

    with patch("commandertui.rclone_bridge.flush_mount_cache"):
        res = batch_transfer_files_with_rclone(
            ["a.txt", "b.txt"],
            str(fake_mountpoint),
            str(local_dst),
            kind=OpKind.COPY,
            mounts=mounts,
        )

    assert res.ok is True
    assert (local_dst / "a.txt").read_text(encoding="utf-8") == "content-a.txt"
    assert (local_dst / "b.txt").read_text(encoding="utf-8") == "content-b.txt"
    assert not (local_dst / "c.txt").exists()


def test_execute_transfer_plan_delegates_to_rclone_when_mounted(tmp_path: Path) -> None:
    """execute_transfer_plan uses rclone turbo path when source is on an rclone mount."""
    fake_cloud_backing = tmp_path / "cloud_backing"
    fake_mountpoint = tmp_path / "mnt_gdrive"
    local_dst = tmp_path / "local_dst"
    local_dst.mkdir()

    for root in (fake_cloud_backing, fake_mountpoint):
        (root / "folder").mkdir(parents=True)
        (root / "folder" / "one.txt").write_text("111", encoding="utf-8")
        (root / "folder" / "two.txt").write_text("2222", encoding="utf-8")
        (root / "top.txt").write_text("top!", encoding="utf-8")

    mounts = [RcloneMount(mountpoint=str(fake_mountpoint), remote=str(fake_cloud_backing))]

    sources = [str(fake_mountpoint / "folder"), str(fake_mountpoint / "top.txt")]
    plan = build_transfer_plan(
        sources,
        str(local_dst),
        OpKind.COPY,
        detect_conflicts(sources, str(local_dst)),
        ConflictPolicy(Resolution.OVERWRITE, {}),
    )

    with (
        patch("commandertui.rclone_bridge.detect_rclone_mounts", return_value=mounts),
        patch("commandertui.rclone_bridge.flush_mount_cache"),
    ):
        report = execute_transfer_plan(plan, OpKind.COPY)

    assert report.failed == []
    assert (local_dst / "folder" / "one.txt").read_text(encoding="utf-8") == "111"
    assert (local_dst / "folder" / "two.txt").read_text(encoding="utf-8") == "2222"
    assert (local_dst / "top.txt").read_text(encoding="utf-8") == "top!"


def test_executor_delegates_copy_to_rclone_when_mounted(tmp_path: Path) -> None:
    """executor.execute routes COPY actions on rclone mounts through batch_transfer_files_with_rclone."""
    fake_cloud_backing = tmp_path / "cloud_backing"
    fake_mountpoint = tmp_path / "mnt_gdrive"
    local_dst = tmp_path / "local_dst"
    local_dst.mkdir()

    for root in (fake_cloud_backing, fake_mountpoint):
        root.mkdir(parents=True, exist_ok=True)
        (root / "notes.md").write_text("# Notes", encoding="utf-8")

    mounts = [RcloneMount(mountpoint=str(fake_mountpoint), remote=str(fake_cloud_backing))]
    diffs = compare(str(fake_mountpoint), str(local_dst))
    queue = plan_mirror(diffs)

    with (
        patch("commandertui.rclone_bridge.detect_rclone_mounts", return_value=mounts),
        patch("commandertui.rclone_bridge.flush_mount_cache"),
    ):
        report = execute(queue, str(fake_mountpoint), str(local_dst))

    assert report.failed == []
    assert (local_dst / "notes.md").read_text(encoding="utf-8") == "# Notes"


def test_rclone_cancellation_returns_cancelled_result(tmp_path: Path) -> None:
    """When cancel_check returns True, copy_with_rclone cancels cleanly."""
    fake_cloud_backing = tmp_path / "cloud_backing"
    fake_cloud_backing.mkdir()
    fake_mountpoint = tmp_path / "mnt_gdrive"
    fake_mountpoint.mkdir()
    (fake_cloud_backing / "f.txt").write_text("data", encoding="utf-8")
    (fake_mountpoint / "f.txt").write_text("data", encoding="utf-8")

    mounts = [RcloneMount(mountpoint=str(fake_mountpoint), remote=str(fake_cloud_backing))]
    res = copy_with_rclone(
        str(fake_mountpoint / "f.txt"),
        str(tmp_path / "out.txt"),
        is_dir=False,
        cancel_check=lambda: True,
        mounts=mounts,
    )

    assert res.ok is False
    assert res.cancelled is True


def test_rclone_progress_compatible_with_transfer_status(tmp_path: Path) -> None:
    """Progress callbacks emitted by copy_with_rclone keep TransferStatus monotonic and accurate."""
    from commandertui.executor import TransferStatus

    fake_cloud_backing = tmp_path / "cloud_backing"
    fake_mountpoint = tmp_path / "mnt_gdrive"
    local_dst = tmp_path / "local_dst"

    for root in (fake_cloud_backing, fake_mountpoint):
        (root / "dir").mkdir(parents=True)
        (root / "dir" / "a.txt").write_text("A" * 1000, encoding="utf-8")
        (root / "dir" / "b.txt").write_text("B" * 2000, encoding="utf-8")

    mounts = [RcloneMount(mountpoint=str(fake_mountpoint), remote=str(fake_cloud_backing))]
    status = TransferStatus(files_total=2, bytes_total=3000)
    current_done = 0

    def on_progress(done: int, total: int, name: str) -> None:
        nonlocal current_done
        if done == 0:
            status.files_done += 1
            current_done = 0
        status.bytes_done += done - current_done
        current_done = done
        status.current_file = name

    with patch("commandertui.rclone_bridge.flush_mount_cache"):
        res = copy_with_rclone(
            str(fake_mountpoint / "dir"),
            str(local_dst / "dir"),
            is_dir=True,
            progress=on_progress,
            mounts=mounts,
        )

    assert res.ok is True
    assert status.files_done == 2
    assert status.bytes_done == 3000
