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
