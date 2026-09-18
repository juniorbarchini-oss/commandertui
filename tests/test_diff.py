import os
import time

import pytest

from commandertui.diff import compare
from commandertui.models import DiffStatus


def _write(path, content):
    with open(path, "w") as f:
        f.write(content)


def test_left_only_and_right_only(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    _write(left / "only_left.txt", "a")
    _write(right / "only_right.txt", "b")

    diffs = {d.rel_path: d.status for d in compare(str(left), str(right))}

    assert diffs["only_left.txt"] == DiffStatus.LEFT_ONLY
    assert diffs["only_right.txt"] == DiffStatus.RIGHT_ONLY


def test_identical_files(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    _write(left / "same.txt", "identical content")
    _write(right / "same.txt", "identical content")
    now = time.time()
    os.utime(left / "same.txt", (now, now))
    os.utime(right / "same.txt", (now, now))

    diffs = {d.rel_path: d.status for d in compare(str(left), str(right))}

    assert diffs["same.txt"] == DiffStatus.IDENTICAL


def test_modified_by_size(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    _write(left / "f.txt", "short")
    _write(right / "f.txt", "a much longer version of the file")

    diffs = {d.rel_path: d.status for d in compare(str(left), str(right))}

    assert diffs["f.txt"] == DiffStatus.MODIFIED


def test_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        compare(str(tmp_path / "nope"), str(tmp_path))
