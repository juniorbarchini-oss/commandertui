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


def _copy_queue(tmp_path):
    from commandertui.models import OpKind, OpSide, OperationQueue, QueuedOp

    left, right = tmp_path / "left", tmp_path / "right"
    (left / "dir").mkdir(parents=True)
    right.mkdir()
    (left / "dir" / "a.bin").write_bytes(b"a" * 3000)
    (left / "dir" / "b.bin").write_bytes(b"b" * 1000)
    (left / "c.bin").write_bytes(b"c" * 500)
    queue = OperationQueue()
    queue.add(QueuedOp(OpKind.COPY, OpSide.LEFT_TO_RIGHT, "dir", True))
    queue.add(QueuedOp(OpKind.COPY, OpSide.LEFT_TO_RIGHT, "c.bin", False))
    return left, right, queue


def test_transfer_status_tracks_bytes_and_files(tmp_path, monkeypatch):
    import commandertui.sync as sync
    from dataclasses import replace

    monkeypatch.setattr(sync, "CHUNK_SIZE", 256)
    left, right, queue = _copy_queue(tmp_path)
    seen = []

    report = execute(queue, str(left), str(right), on_transfer=lambda s: seen.append(replace(s)))

    assert report.succeeded == 2
    assert (seen[0].files_total, seen[0].bytes_total) == (3, 4500)
    assert [s.bytes_done for s in seen] == sorted(s.bytes_done for s in seen)
    assert (seen[-1].files_done, seen[-1].bytes_done) == (3, 4500)
    assert any(0 < s.file_done < s.file_total for s in seen)


def test_cancel_stops_queue_and_reports_it(tmp_path):
    left, right, queue = _copy_queue(tmp_path)

    report = execute(queue, str(left), str(right), cancel_check=lambda: True)

    assert report.cancelled and report.succeeded == 0
    assert not (right / "c.bin").exists()
