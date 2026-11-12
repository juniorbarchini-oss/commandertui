"""Unit tests for archive creation and extraction (.tar.gz / .zip)."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from commandertui.archive import (
    archive_uncompressed_stats,
    create_tar_gz,
    default_archive_name,
    extract_archive,
    is_supported_archive,
    strip_archive_extension,
)


def test_archive_helpers() -> None:
    assert is_supported_archive("/tmp/backup.tar.gz") is True
    assert is_supported_archive("/tmp/backup.TGZ") is True
    assert is_supported_archive("/tmp/backup.zip") is True
    assert is_supported_archive("/tmp/notes.txt") is False

    assert strip_archive_extension("cines_backup.tar.gz") == "cines_backup"
    assert strip_archive_extension("data.tgz") == "data"
    assert strip_archive_extension("docs.zip") == "docs"

    assert default_archive_name(["/mnt/data/cines_backup"], "/mnt/data") == "cines_backup.tar.gz"
    assert default_archive_name(["/mnt/data/a.txt", "/mnt/data/b.txt"], "/mnt/data") == "data.tar.gz"


def test_create_and_extract_tar_gz_roundtrip(tmp_path: Path) -> None:
    """create_tar_gz packs a folder tree and extract_archive restores it with progress."""
    src_root = tmp_path / "src"
    folder = src_root / "project"
    (folder / "sub").mkdir(parents=True)
    (folder / "readme.md").write_text("# Hello\n", encoding="utf-8")
    (folder / "sub" / "code.py").write_text("print(42)\n", encoding="utf-8")

    out_archive = tmp_path / "dst" / "project.tar.gz"
    create_events: list[tuple[int, int, str]] = []

    res = create_tar_gz(
        [str(folder)],
        str(out_archive),
        progress=lambda d, t, n: create_events.append((d, t, n)),
    )

    assert res.ok is True
    assert res.cancelled is False
    assert res.files_processed == 2
    assert out_archive.is_file()
    assert len(create_events) > 0

    count, total_bytes = archive_uncompressed_stats(str(out_archive))
    assert count == 2
    assert total_bytes == len("# Hello\n") + len("print(42)\n")

    extract_dir = tmp_path / "extracted"
    extract_events: list[tuple[int, int, str]] = []
    ext_res = extract_archive(
        str(out_archive),
        str(extract_dir),
        progress=lambda d, t, n: extract_events.append((d, t, n)),
    )

    assert ext_res.ok is True
    assert ext_res.files_processed == 2
    assert (extract_dir / "project" / "readme.md").read_text(encoding="utf-8") == "# Hello\n"
    assert (extract_dir / "project" / "sub" / "code.py").read_text(encoding="utf-8") == "print(42)\n"
    assert len(extract_events) > 0


def test_create_tar_gz_cancellation_removes_partial_file(tmp_path: Path) -> None:
    """Cancelling create_tar_gz leaves no output or temporary file behind."""
    src_file = tmp_path / "big.txt"
    src_file.write_text("X" * 10000, encoding="utf-8")
    out_archive = tmp_path / "out.tar.gz"

    res = create_tar_gz(
        [str(src_file)],
        str(out_archive),
        cancel_check=lambda: True,
    )

    assert res.ok is False
    assert res.cancelled is True
    assert not out_archive.exists()


def test_extract_archive_rejects_path_traversal(tmp_path: Path) -> None:
    """extract_archive refuses tar members containing '../'."""
    bad_tar = tmp_path / "evil.tar.gz"
    with tarfile.open(bad_tar, mode="w:gz") as tf:
        data = b"pwned"
        info = tarfile.TarInfo(name="../escaped.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    target_dir = tmp_path / "safe_dir"
    res = extract_archive(str(bad_tar), str(target_dir))

    assert res.ok is False
    assert res.error is not None and "Unsafe archive member" in res.error
    assert not (tmp_path / "escaped.txt").exists()


def test_extract_archive_conflict_skip_and_replace_tar(tmp_path: Path) -> None:
    """extract_archive respects conflict_policy='skip' and 'replace' with trash for tar archives."""
    from unittest.mock import patch
    from commandertui.archive import get_archive_existing_targets

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "file.txt").write_text("new content", encoding="utf-8")
    arc_path = tmp_path / "test.tar.gz"
    create_tar_gz([str(src_dir / "file.txt")], str(arc_path))

    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()
    existing_file = dst_dir / "file.txt"
    existing_file.write_text("old content", encoding="utf-8")

    conflicts = get_archive_existing_targets(str(arc_path), str(dst_dir))
    assert conflicts == [str(existing_file)]

    # Test conflict_policy="skip"
    res_skip = extract_archive(str(arc_path), str(dst_dir), conflict_policy="skip")
    assert res_skip.ok is True
    assert existing_file.read_text(encoding="utf-8") == "old content"

    # Test conflict_policy="replace"
    trashed: list[str] = []
    with patch("commandertui.archive.send_to_trash", side_effect=lambda p: trashed.append(p) or True):
        res_replace = extract_archive(str(arc_path), str(dst_dir), conflict_policy="replace")

    assert res_replace.ok is True
    assert trashed == [str(existing_file)]
    assert existing_file.read_text(encoding="utf-8") == "new content"


def test_extract_archive_conflict_skip_and_replace_zip(tmp_path: Path) -> None:
    """extract_archive respects conflict_policy='skip' and 'replace' with trash for zip archives."""
    import zipfile
    from unittest.mock import patch
    from commandertui.archive import get_archive_existing_targets

    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("notes.txt", "zip new content")

    dst_dir = tmp_path / "dst_zip"
    dst_dir.mkdir()
    existing_file = dst_dir / "notes.txt"
    existing_file.write_text("zip old content", encoding="utf-8")

    conflicts = get_archive_existing_targets(str(zip_path), str(dst_dir))
    assert conflicts == [str(existing_file)]

    res_skip = extract_archive(str(zip_path), str(dst_dir), conflict_policy="skip")
    assert res_skip.ok is True
    assert existing_file.read_text(encoding="utf-8") == "zip old content"

    trashed: list[str] = []
    with patch("commandertui.archive.send_to_trash", side_effect=lambda p: trashed.append(p) or True):
        res_replace = extract_archive(str(zip_path), str(dst_dir), conflict_policy="replace")

    assert res_replace.ok is True
    assert trashed == [str(existing_file)]
    assert existing_file.read_text(encoding="utf-8") == "zip new content"


def test_create_tar_gz_on_rclone_mount_does_not_double_counts(tmp_path: Path) -> None:
    """Creating an archive destined for an rclone mount counts exact files and bytes."""
    from unittest.mock import patch
    from commandertui.rclone_bridge import RcloneMount, RcloneResult

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.txt").write_text("AAA", encoding="utf-8")
    (src_dir / "b.txt").write_text("BBBB", encoding="utf-8")

    fake_cloud = tmp_path / "cloud_backing"
    fake_mount = tmp_path / "mnt_gdrive"
    fake_mount.mkdir()
    out_arc = fake_mount / "backup.tar.gz"

    mounts = [RcloneMount(mountpoint=str(fake_mount), remote=str(fake_cloud))]
    progress_calls: list[tuple[int, int, str]] = []

    with (
        patch("commandertui.archive.detect_rclone_mounts", return_value=mounts),
        patch("commandertui.archive.rclone_available", return_value=True),
        patch(
            "commandertui.archive.copy_with_rclone",
            return_value=RcloneResult(ok=True, files_transferred=1, bytes_transferred=100),
        ),
    ):
        res = create_tar_gz(
            [str(src_dir / "a.txt"), str(src_dir / "b.txt")],
            str(out_arc),
            progress=lambda d, t, n: progress_calls.append((d, t, n)),
        )

    assert res.ok is True
    assert res.files_processed == 2
    assert res.bytes_processed == 7
