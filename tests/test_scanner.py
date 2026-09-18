import os

from commandertui.models import EntryKind
from commandertui.scanner import path_size, scan_directory


def test_scan_flat_files(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.txt").write_text("world!")

    result = scan_directory(str(tmp_path))

    assert set(result) == {"a.txt", "b.txt"}
    assert result["a.txt"].kind == EntryKind.FILE
    assert result["a.txt"].size == 5
    assert result["b.txt"].size == 6


def test_scan_nested_dirs(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("x")

    result = scan_directory(str(tmp_path))

    assert "sub" in result and result["sub"].kind == EntryKind.DIR
    assert "sub/nested.txt" in result


def test_scan_excludes_default_patterns(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "keep.txt").write_text("x")

    result = scan_directory(str(tmp_path))

    assert ".git" not in result
    assert "keep.txt" in result


def test_scan_missing_dir_returns_empty():
    result = scan_directory("/no/such/path/at/all")
    assert result == {}


def test_path_size_of_a_single_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("12345")
    assert path_size(str(f)) == 5


def test_path_size_sums_a_directory_tree(tmp_path):
    (tmp_path / "a.txt").write_text("12345")  # 5 bytes
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("1234567")  # 7 bytes

    assert path_size(str(tmp_path)) == 12
