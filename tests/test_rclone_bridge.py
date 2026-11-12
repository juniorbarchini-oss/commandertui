"""Unit tests for the rclone Turbo Copy bridge and its integration with transfer/executor."""

from __future__ import annotations

from pathlib import Path

import pytest
from unittest.mock import patch

from commandertui.conflicts import Resolution, detect_conflicts
from commandertui.diff import compare
from commandertui.executor import execute
from commandertui.models import OpKind
from commandertui.plan import plan_mirror
from commandertui.rclone_bridge import (
    RcloneMount,
    RcloneResult,
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


def test_find_rclone_mount_pids_and_flush_cache(tmp_path: Path) -> None:
    """find_rclone_mount_pids only matches rclone mount processes for the specified mountpoint."""
    import signal
    from commandertui.rclone_bridge import find_rclone_mount_pids, flush_mount_cache

    proc_dir = tmp_path / "proc"
    (proc_dir / "101").mkdir(parents=True)
    (proc_dir / "101" / "cmdline").write_bytes(b"/usr/bin/rclone\x00mount\x00gdrive:\x00/mnt/gdrive\x00")

    (proc_dir / "102").mkdir()
    (proc_dir / "102" / "cmdline").write_bytes(b"/usr/bin/rclone\x00copy\x00/tmp/a\x00/mnt/gdrive\x00")

    (proc_dir / "103").mkdir()
    (proc_dir / "103" / "cmdline").write_bytes(b"/usr/bin/rclone\x00serve\x00http\x00/mnt/gdrive\x00")

    (proc_dir / "104").mkdir()
    (proc_dir / "104" / "cmdline").write_bytes(b"/usr/bin/rclone\x00mount\x00backup:\x00/mnt/backup\x00")

    (proc_dir / "105").mkdir()
    (proc_dir / "105" / "cmdline").write_bytes(b"python3\x00server.py\x00")

    matched_gdrive = find_rclone_mount_pids(["/mnt/gdrive"], proc_dir=str(proc_dir))
    assert matched_gdrive == [101]

    matched_all = find_rclone_mount_pids(proc_dir=str(proc_dir))
    assert matched_all == [101, 104]

    with patch("os.kill") as mock_kill:
        signalled = flush_mount_cache(["/mnt/gdrive"], proc_dir=str(proc_dir))
        assert signalled == [101]
        mock_kill.assert_called_once_with(101, signal.SIGHUP)


def test_rclone_config_error_fallback(tmp_path: Path, monkeypatch) -> None:
    """When rclone fails with a config/remote-not-found error, execution falls back to normal motor."""
    import os

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_rclone = fake_bin / "rclone"
    fake_rclone.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "sys.stderr.write(json.dumps({'level': 'error', 'msg': 'Failed to create file system for \"bad:\": didn\\'t find section in config file'}) + '\\n')\n"
        "sys.exit(1)\n"
    )
    fake_rclone.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "doc.txt").write_text("fallback content", encoding="utf-8")
    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()

    mounts = [RcloneMount(mountpoint=str(dst_dir), remote="bad:")]
    sources = [str(src_dir / "doc.txt")]
    plan = build_transfer_plan(
        sources,
        str(dst_dir),
        OpKind.COPY,
        detect_conflicts(sources, str(dst_dir)),
        ConflictPolicy(Resolution.OVERWRITE, {}),
    )

    with patch("commandertui.rclone_bridge.detect_rclone_mounts", return_value=mounts):
        report = execute_transfer_plan(plan, OpKind.COPY)

    assert report.failed == []
    assert report.copied == 1
    assert (dst_dir / "doc.txt").read_text(encoding="utf-8") == "fallback content"


def test_symlinks_in_local_dir_bypasses_rclone(tmp_path: Path) -> None:
    """Copying a local folder containing symlinks bypasses rclone and uses normal motor."""
    src_dir = tmp_path / "src_tree"
    src_dir.mkdir()
    (src_dir / "real.txt").write_text("real content", encoding="utf-8")
    (src_dir / "link.txt").symlink_to("real.txt")

    dst_dir = tmp_path / "dst_mount"
    dst_dir.mkdir()

    mounts = [RcloneMount(mountpoint=str(dst_dir), remote="gdrive:")]
    sources = [str(src_dir)]
    plan = build_transfer_plan(
        sources,
        str(dst_dir),
        OpKind.COPY,
        detect_conflicts(sources, str(dst_dir)),
        ConflictPolicy(Resolution.OVERWRITE, {}),
    )

    with (
        patch("commandertui.rclone_bridge.detect_rclone_mounts", return_value=mounts),
        patch("commandertui.rclone_bridge.copy_with_rclone") as mock_rclone_copy,
    ):
        report = execute_transfer_plan(plan, OpKind.COPY)

    assert report.failed == []
    mock_rclone_copy.assert_not_called()
    assert (dst_dir / "src_tree" / "real.txt").read_text(encoding="utf-8") == "real content"
    assert (dst_dir / "src_tree" / "link.txt").is_symlink()


def test_batch_transfer_reports_partial_failures_accurately(tmp_path: Path) -> None:
    """When a batch has failing items, failed items are recorded and successful items are counted."""
    from commandertui.rclone_bridge import RcloneResult

    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "ok1.txt").write_text("1", encoding="utf-8")
    (src_root / "fail.txt").write_text("2", encoding="utf-8")
    (src_root / "ok2.txt").write_text("3", encoding="utf-8")

    dst_root = tmp_path / "dst"
    dst_root.mkdir()

    mounts = [RcloneMount(mountpoint=str(dst_root), remote="gdrive:")]
    sources = [str(src_root / "ok1.txt"), str(src_root / "fail.txt"), str(src_root / "ok2.txt")]
    plan = build_transfer_plan(
        sources,
        str(dst_root),
        OpKind.COPY,
        detect_conflicts(sources, str(dst_root)),
        ConflictPolicy(Resolution.OVERWRITE, {}),
    )

    fake_result = RcloneResult(
        ok=False,
        files_transferred=2,
        bytes_transferred=2,
        error="fail.txt: permission denied",
        failed=[("fail.txt", "permission denied")],
        succeeded=["ok1.txt", "ok2.txt"],
    )

    with (
        patch("commandertui.rclone_bridge.detect_rclone_mounts", return_value=mounts),
        patch("commandertui.rclone_bridge.batch_transfer_files_with_rclone", return_value=fake_result),
    ):
        report = execute_transfer_plan(plan, OpKind.COPY)

    assert report.copied == 2
    assert len(report.failed) == 1
    assert report.failed[0][0] == str(src_root / "fail.txt")
    assert "permission denied" in report.failed[0][1]


def test_rclone_transient_error_retry_success(tmp_path: Path, monkeypatch) -> None:
    """When rclone logs a transient error but recovers and exits 0, the result is ok and failed is empty."""
    import os

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_rclone = fake_bin / "rclone"
    fake_rclone.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "sys.stderr.write(json.dumps({'level': 'error', 'msg': 'rateLimitExceeded 403', 'object': 'file.txt'}) + '\\n')\n"
        "sys.stderr.write(json.dumps({'level': 'info', 'msg': 'Copied (new)', 'object': 'file.txt'}) + '\\n')\n"
        "sys.stderr.write(json.dumps({'level': 'info', 'msg': '', 'stats': {'bytes': 100, 'transfers': 1}}) + '\\n')\n"
        "sys.exit(0)\n"
    )
    fake_rclone.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "file.txt").write_text("hello", encoding="utf-8")
    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()

    mounts = [RcloneMount(mountpoint=str(dst_dir), remote="gdrive:")]
    with patch("commandertui.rclone_bridge.flush_mount_cache"):
        res = copy_with_rclone(
            str(src_dir / "file.txt"),
            str(dst_dir / "file.txt"),
            is_dir=False,
            mounts=mounts,
        )

    assert res.ok is True
    assert res.failed == []


def test_batch_move_partial_failure_trashes_succeeded_sources(tmp_path: Path) -> None:
    """In a partial failure move, files that were transferred successfully are sent to trash."""
    src_root = tmp_path / "src"
    src_root.mkdir()
    f1 = src_root / "ok1.txt"
    f2 = src_root / "ok2.txt"
    f3 = src_root / "fail.txt"
    f1.write_text("1", encoding="utf-8")
    f2.write_text("2", encoding="utf-8")
    f3.write_text("3", encoding="utf-8")

    dst_root = tmp_path / "dst"
    dst_root.mkdir()

    mounts = [RcloneMount(mountpoint=str(dst_root), remote="gdrive:")]
    trashed: list[str] = []

    def _fake_run_rclone(cmd, progress, cancel_check, default_name):
        return RcloneResult(
            ok=False,
            files_transferred=2,
            bytes_transferred=2,
            error="fail.txt: permission denied",
            failed=[("fail.txt", "permission denied")],
            succeeded=["ok1.txt", "ok2.txt"],
        )

    with (
        patch("commandertui.rclone_bridge._run_rclone_process", side_effect=_fake_run_rclone),
        patch("commandertui.rclone_bridge.send_to_trash", side_effect=lambda p: trashed.append(p) or True),
        patch("commandertui.rclone_bridge.flush_mount_cache"),
    ):
        res = batch_transfer_files_with_rclone(
            ["ok1.txt", "ok2.txt", "fail.txt"],
            str(src_root),
            str(dst_root),
            kind=OpKind.MOVE,
            mounts=mounts,
        )

    assert res.ok is False
    assert len(res.failed) == 1
    assert res.failed[0][0] == "fail.txt"
    assert str(f1) in trashed
    assert str(f2) in trashed
    assert str(f3) not in trashed


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("silent_failure", []),
        ("cancelled", []),
        ("partial_copy", ["a.txt"]),
        ("partial_move", ["a.txt"]),
        ("success", ["a.txt", "b.txt"]),
        ("unconfirmed_error", []),
    ],
)
def test_batch_move_only_trashes_confirmed_uploads(tmp_path, monkeypatch, scenario, expected):
    import os

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    bin_dir = tmp_path / "bin"
    for directory in (src, dst, bin_dir):
        directory.mkdir()
    for name in ("a.txt", "b.txt"):
        (src / name).write_text(name)
    lines = ["#!/usr/bin/env python3", "import json, sys, time"]
    if scenario in {"partial_copy", "partial_move", "cancelled"}:
        verb = "Moved" if scenario == "partial_move" else "Copied"
        lines.append(
            f"print(json.dumps({{'level': 'info', 'msg': '{verb} (new)', 'object': 'a.txt'}}), file=sys.stderr, flush=True)"
        )
    if scenario == "unconfirmed_error":
        lines.append(
            "print(json.dumps({'level': 'error', 'msg': 'denied', 'object': 'b.txt'}), file=sys.stderr, flush=True)"
        )
    if scenario == "cancelled":
        lines.append("time.sleep(20)")
    lines.append("sys.exit(0)" if scenario == "success" else "sys.exit(1)")
    fake = bin_dir / "rclone"
    fake.write_text("\n".join(lines) + "\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    cancel = False

    def progress(*args):
        nonlocal cancel
        cancel = scenario == "cancelled"

    trashed = []
    with (
        patch(
            "commandertui.rclone_bridge.send_to_trash",
            side_effect=lambda p: trashed.append(Path(p).name) or True,
        ),
        patch("commandertui.rclone_bridge.flush_mount_cache"),
    ):
        result = batch_transfer_files_with_rclone(
            ["a.txt", "b.txt"],
            str(src),
            str(dst),
            OpKind.MOVE,
            mounts=[RcloneMount(str(dst), "fake:")],
            progress=progress,
            cancel_check=lambda: cancel,
        )
    assert trashed == expected
    assert result.succeeded == expected
    assert result.files_transferred == len(expected)
    assert result.cancelled == (scenario == "cancelled")
    assert result.ok == (scenario == "success")
    if not result.cancelled:
        assert {name for name, _ in result.failed} == {"a.txt", "b.txt"} - set(expected)
    assert all((src / name).read_text() == name for name in ("a.txt", "b.txt"))


def test_batch_move_trash_failure_is_not_reported_as_moved(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    for name in ("a.txt", "b.txt"):
        (src / name).write_text(name)
    with (
        patch("commandertui.rclone_bridge._run_rclone_process", return_value=RcloneResult(ok=True)),
        patch("commandertui.rclone_bridge.send_to_trash", side_effect=[False, True]) as trash,
        patch("commandertui.rclone_bridge.flush_mount_cache"),
    ):
        result = batch_transfer_files_with_rclone(
            ["a.txt", "b.txt"],
            str(src),
            str(dst),
            OpKind.MOVE,
            mounts=[RcloneMount(str(dst), "fake:")],
        )
    assert trash.call_count == 2
    assert not result.ok
    assert result.succeeded == ["b.txt"]
    assert result.files_transferred == 1
    assert result.failed == [("a.txt", f"Copied but could not trash source: {src / 'a.txt'}")]


@pytest.mark.parametrize("engine", ["executor", "transfer"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_batch_reports_require_confirmed_successes(tmp_path, engine, cancelled):
    from commandertui.models import OperationQueue, QueuedOp, OpSide

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    names = ["a.txt", "b.txt", "c.txt"]
    for name in names:
        (src / name).write_text(name)
    result = RcloneResult(
        ok=False,
        cancelled=cancelled,
        succeeded=["a.txt"],
        failed=[("b.txt", "denied")],
        error="batch interrupted",
    )
    mounts = [RcloneMount(str(dst), "fake:")]
    with (
        patch("commandertui.rclone_bridge.rclone_available", return_value=True),
        patch("commandertui.rclone_bridge.detect_rclone_mounts", return_value=mounts),
        patch("commandertui.rclone_bridge.batch_transfer_files_with_rclone", return_value=result),
    ):
        if engine == "executor":
            queue = OperationQueue(
                [QueuedOp(OpKind.MOVE, OpSide.LEFT_TO_RIGHT, name, False) for name in names]
            )
            report = execute(queue, str(src), str(dst))
            count = report.succeeded
        else:
            sources = [str(src / name) for name in names]
            plan = build_transfer_plan(
                sources,
                str(dst),
                OpKind.MOVE,
                detect_conflicts(sources, str(dst)),
                ConflictPolicy(Resolution.OVERWRITE, {}),
            )
            report = execute_transfer_plan(plan, OpKind.MOVE)
            count = report.copied
    assert count == (0 if cancelled else 1)
    assert report.cancelled == cancelled
    if not cancelled:
        assert {Path(name).name for name, _ in report.failed} == {"b.txt", "c.txt"}
