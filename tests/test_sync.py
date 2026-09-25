import os

from commandertui.sync import copy_item, delete_item, move_item, verify_copy


def test_copy_file_preserves_content_and_mode(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("payload")
    os.chmod(src, 0o644)
    dst = tmp_path / "out" / "dst.txt"

    result = copy_item(str(src), str(dst))

    assert result.ok
    assert dst.read_text() == "payload"
    assert not any(p.name.endswith(".tmp") for p in dst.parent.iterdir())


def test_copy_no_partial_file_left_behind_on_read_error(tmp_path, monkeypatch):
    src = tmp_path / "src.txt"
    src.write_text("payload")
    dst = tmp_path / "dst.txt"

    def boom(*a, **k):
        raise OSError("disk yanked mid-write")

    import commandertui.sync as sync_mod

    monkeypatch.setattr(sync_mod.shutil, "copystat", boom)
    result = copy_item(str(src), str(dst))

    assert not result.ok
    assert not dst.exists()
    assert not any(f.endswith(".tmp") for f in os.listdir(tmp_path))


def test_copy_directory_tree(tmp_path):
    src = tmp_path / "srcdir"
    (src / "nested").mkdir(parents=True)
    (src / "a.txt").write_text("a")
    (src / "nested" / "b.txt").write_text("b")
    dst = tmp_path / "dstdir"

    result = copy_item(str(src), str(dst))

    assert result.ok
    assert (dst / "a.txt").read_text() == "a"
    assert (dst / "nested" / "b.txt").read_text() == "b"


def test_move_same_filesystem_is_atomic_rename(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("x")
    dst = tmp_path / "b.txt"

    result = move_item(str(src), str(dst))

    assert result.ok
    assert not src.exists()
    assert dst.read_text() == "x"


def test_delete_goes_to_trash_not_gone_forever(tmp_path, monkeypatch):
    target = tmp_path / "gone.txt"
    target.write_text("bye")

    calls = []

    def fake_send2trash(path):
        calls.append(path)
        os.remove(path)

    import commandertui.trash as trash_mod

    monkeypatch.setitem(
        __import__("sys").modules,
        "send2trash",
        type("m", (), {"send2trash": staticmethod(fake_send2trash)}),
    )

    result = delete_item(str(target))

    assert result.ok
    assert calls == [str(target)]
    assert not target.exists()


def test_verify_copy_detects_mismatch(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("payload")
    dst = tmp_path / "dst.txt"
    dst.write_text("different")

    assert verify_copy(str(src), str(dst)) is False

    dst.write_text("payload")
    assert verify_copy(str(src), str(dst)) is True


def test_cancel_mid_file_leaves_no_partial_and_no_temp(tmp_path, monkeypatch):
    import commandertui.sync as sync

    monkeypatch.setattr(sync, "CHUNK_SIZE", 4)
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 40)
    dst = tmp_path / "out" / "big.bin"
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3

    result = sync.copy_item(str(src), str(dst), cancel_check=cancel)

    assert result.cancelled and not result.ok
    assert not dst.exists()
    assert list((tmp_path / "out").iterdir()) == []


def test_cancelled_move_keeps_source(tmp_path, monkeypatch):
    import commandertui.sync as sync

    monkeypatch.setattr(sync.os, "rename", lambda a, b: (_ for _ in ()).throw(OSError("cross-device")))
    src = tmp_path / "a.txt"
    src.write_text("data")

    result = sync.move_item(str(src), str(tmp_path / "b" / "a.txt"), cancel_check=lambda: True)

    assert result.cancelled
    assert src.read_text() == "data"
