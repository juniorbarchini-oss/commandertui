from commandertui.diff import compare
from commandertui.models import OpKind
from commandertui.plan import plan_mirror, plan_two_way, plan_update


def _setup(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "new_in_left.txt").write_text("a")
    (right / "stale_in_right.txt").write_text("b")
    (left / "both.txt").write_text("left version")
    (right / "both.txt").write_text("right version, different")
    return left, right


def test_mirror_copies_left_and_deletes_right_only(tmp_path):
    left, right = _setup(tmp_path)
    diffs = compare(str(left), str(right))

    queue = plan_mirror(diffs)
    kinds = {(op.kind, op.rel_path) for op in queue.ops}

    assert (OpKind.COPY, "new_in_left.txt") in kinds
    assert (OpKind.COPY, "both.txt") in kinds
    assert (OpKind.DELETE, "stale_in_right.txt") in kinds


def test_update_never_deletes(tmp_path):
    left, right = _setup(tmp_path)
    diffs = compare(str(left), str(right))

    queue = plan_update(diffs)

    assert all(op.kind != OpKind.DELETE for op in queue.ops)
    assert any(op.rel_path == "new_in_left.txt" for op in queue.ops)


def test_two_way_surfaces_conflicts_without_guessing(tmp_path):
    left, right = _setup(tmp_path)
    diffs = compare(str(left), str(right))

    queue, conflicts = plan_two_way(diffs)

    assert len(conflicts) == 1
    assert conflicts[0].rel_path == "both.txt"
    assert all(op.rel_path != "both.txt" for op in queue.ops)
    assert any(op.rel_path == "new_in_left.txt" for op in queue.ops)
    assert any(op.rel_path == "stale_in_right.txt" for op in queue.ops)
