from commandertui.diff import compare
from commandertui.executor import execute
from commandertui.plan import plan_mirror


def test_execute_mirror_makes_right_match_left(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "keep.txt").write_text("content")
    (right / "remove_me.txt").write_text("stale")

    diffs = compare(str(left), str(right))
    queue = plan_mirror(diffs)

    report = execute(queue, str(left), str(right))

    assert report.failed == []
    assert (right / "keep.txt").read_text() == "content"
    assert not (right / "remove_me.txt").exists()


def test_execute_reports_progress(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "a.txt").write_text("a")
    (left / "b.txt").write_text("b")

    diffs = compare(str(left), str(right))
    queue = plan_mirror(diffs)

    calls = []
    execute(queue, str(left), str(right), on_progress=lambda i, t, name: calls.append((i, t, name)))

    assert calls[-1][0] == calls[-1][1] == len(queue)
