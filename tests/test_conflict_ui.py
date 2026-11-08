import os

import pytest
from textual.widgets import Button, Checkbox

from commandertui.app import CommanderApp
from commandertui.conflicts import detect_conflicts
from commandertui.modals import ConfirmScreen, ConflictScreen, MessageScreen
from commandertui.models import OpKind


def write(path, text="new", mtime=200):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def roots(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    return left, right


async def main(pilot):
    for _ in range(4):
        await pilot.press("enter")
        await pilot.pause()
        if pilot.app.screen.__class__.__name__ != "BootScreen":
            return
    raise AssertionError("Boot did not close")


async def screen(pilot, cls):
    for _ in range(60):
        await pilot.pause(0.05)
        if isinstance(pilot.app.screen, cls):
            return pilot.app.screen
    raise AssertionError(f"Expected {cls}, got {pilot.app.screen}")


@pytest.mark.parametrize("kind", [OpKind.COPY, OpKind.MOVE])
async def test_conflict_buttons_and_safe_focus(roots, kind):
    left, right = roots
    source = write(left / "file")
    write(right / "file", "old", 100)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        results = []
        app.push_screen(
            ConflictScreen(detect_conflicts([str(source)], str(right)), str(right), kind), results.append
        )
        dialog = await screen(pilot, ConflictScreen)
        ids = {button.id for button in dialog.query(Button)}
        assert "conflict-ask" in ids
        assert "conflict-merge" not in ids
        assert ("conflict-only_newer" in ids) == (kind == OpKind.COPY)
        assert 5 <= len(ids) <= 7
        assert dialog.focused.id == "conflict-skip"
        await pilot.press("escape")
        await pilot.pause()
        assert results == [("cancel", False)]
        assert source.read_text() == "new" and (right / "file").read_text() == "old"


@pytest.mark.parametrize("choice,expected", [("s", "old"), ("o", "new"), ("r", "old"), ("n", "new")])
async def test_f5_conflict_flow(roots, choice, expected):
    left, right = roots
    source = write(left / "file")
    write(right / "file", "old", 100)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f5")
        await screen(pilot, ConflictScreen)
        await pilot.press(choice)
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert (right / "file").read_text() == expected
        assert (str(source) in app._panel("left").marked) == (choice == "s")
        if choice == "r":
            assert (right / "file_1").read_text() == "new"
    assert source.read_text() == "new"


async def test_escape_conflict_keeps_marks(roots):
    left, right = roots
    source = write(left / "file")
    write(right / "file", "old", 100)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f5")
        await screen(pilot, ConflictScreen)
        await pilot.press("escape")
        assert str(source) in app._panel("left").marked
        assert (right / "file").read_text() == "old"


@pytest.mark.parametrize("key", ["f5", "f6"])
async def test_ask_each_apply_remaining(roots, isolated_trash, key):
    left, right = roots
    for name in ("a", "b"):
        write(left / name)
        write(right / name, "old", 100)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        app._panel("left").marked.update(str(left / name) for name in ("a", "b"))
        await pilot.press(key)
        await screen(pilot, ConflictScreen)
        await pilot.press("a")
        dialog = await screen(pilot, ConflictScreen)
        assert dialog.rel_path == "a"
        await pilot.click("#apply-remaining")
        assert dialog.query_one(Checkbox).value
        await pilot.press("o")
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert [(right / name).read_text() for name in ("a", "b")] == ["new", "new"]
        assert not app._panel("left").marked


async def test_folder_merge_second_policy(roots):
    left, right = roots
    write(left / "folder" / "file")
    write(left / "folder" / "missing")
    write(right / "folder" / "file", "old", 100)
    write(right / "folder" / "extra", "extra")
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f5")
        dialog = await screen(pilot, ConflictScreen)
        assert dialog.query_one("#conflict-merge", Button)
        assert "keeps extra files" in str(dialog.query_one("#conflict-overwrite", Button).label)
        await pilot.press("m")
        dialog = await screen(pilot, ConflictScreen)
        assert dialog.file_policy
        assert "merge" not in dialog.options and "ask" not in dialog.options
        await pilot.press("s")
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert (right / "folder" / "file").read_text() == "old"
        assert (right / "folder" / "missing").read_text() == "new"
        assert (right / "folder" / "extra").read_text() == "extra"
        assert str(left / "folder") in app._panel("left").marked


@pytest.fixture
def isolated_trash(monkeypatch):
    import subprocess
    import sys
    from commandertui import trash

    # Real fallback, isolated by conftest's HOME; never delegate to a service.
    monkeypatch.setitem(sys.modules, "send2trash", None)
    monkeypatch.setattr(trash.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1))


@pytest.mark.parametrize("choice", ["s", "o", "r"])
async def test_f6_conflict_and_retained_marks(roots, isolated_trash, choice):
    left, right = roots
    source = write(left / "file")
    write(right / "file", "old", 100)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f6")
        dialog = await screen(pilot, ConflictScreen)
        assert "merge" not in dialog.options and "only_newer" not in dialog.options
        assert "ask" in dialog.options
        await pilot.press(choice)
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert source.exists() == (choice == "s")
        assert (str(source) in app._panel("left").marked) == (choice == "s")
        assert (right / "file").read_text() == ("new" if choice == "o" else "old")
        if choice == "r":
            assert (right / "file_1").read_text() == "new"


async def test_f6_without_conflicts(roots, isolated_trash):
    left, right = roots
    source = write(left / "file")
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f6")
        await screen(pilot, ConfirmScreen)
        await pilot.press("y")
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert not source.exists() and (right / "file").read_text() == "new"
        assert not app._panel("left").marked


async def test_f6_identical_message_and_marks(roots, isolated_trash, monkeypatch):
    left, right = roots
    source = write(left / "file", "same")
    write(right / "file", "same")
    app = CommanderApp(str(left), str(right))
    messages = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: messages.append(message))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f6")
        await screen(pilot, ConflictScreen)
        await pilot.press("s")
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert source.read_text() == "same" and str(source) in app._panel("left").marked
        assert any("1 identical files stayed in the source" in message for message in messages)


async def test_ask_each_cancel_discards_decisions(roots):
    left, right = roots
    for name in ("a", "b"):
        write(left / name)
        write(right / name, "old", 100)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        app._panel("left").marked.update(str(left / name) for name in ("a", "b"))
        await pilot.press("f5")
        await screen(pilot, ConflictScreen)
        await pilot.press("a")
        await screen(pilot, ConflictScreen)
        await pilot.press("o")
        dialog = await screen(pilot, ConflictScreen)
        assert dialog.rel_path == "b"
        await pilot.press("escape")
        assert [(right / name).read_text() for name in ("a", "b")] == ["old", "old"]
        assert len(app._panel("left").marked) == 2


async def test_scan_is_responsive_and_cancellable(roots, monkeypatch):
    import threading
    from commandertui import run_transfer
    from commandertui.conflicts import ConflictReport
    from commandertui.progress_screen import ProgressScreen

    left, right = roots
    source = write(left / "file")
    release = threading.Event()
    observed = []

    def slow_scan(sources, destination, cancel_check):
        release.wait(5)
        observed.append(cancel_check())
        return ConflictReport()

    monkeypatch.setattr(run_transfer, "detect_conflicts", slow_scan)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f5")
        progress = await screen(pilot, ProgressScreen)
        try:
            await pilot.press("escape", "y")
            assert progress.cancel_requested
        finally:
            release.set()
        dialog = await screen(pilot, MessageScreen)
        assert "Cancelled." in dialog.text
        assert observed == [True]
        assert source.exists() and not (right / "file").exists()
        assert str(source) in app._panel("left").marked


async def test_conflict_summary_is_bounded_and_lists_duplicates(roots):
    left, right = roots
    sources = []
    for index in range(8):
        sources.append(str(write(left / str(index), mtime=300)))
        write(right / str(index), "old", 100)
    other = write(left / "sub" / "0")
    sources.append(str(other))
    report = detect_conflicts(sources, str(right))
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        app.push_screen(ConflictScreen(report, str(right), OpKind.COPY))
        dialog = await screen(pilot, ConflictScreen)
        text = str(dialog.query_one("#conflict-summary").render())
        assert "... and 3 more" in text and "Repeated source names: 0" in text
        assert "Source:" in text and "Destination:" in text and "newer: src" in text


async def test_f6_folder_partial_skip_keeps_folder_marked(roots, isolated_trash):
    left, right = roots
    retained = write(left / "folder" / "keep")
    moved = write(left / "folder" / "missing")
    write(right / "folder" / "keep", "old", 100)
    write(right / "folder" / "extra", "extra")
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f6")
        dialog = await screen(pilot, ConflictScreen)
        assert "merge" not in dialog.options and "only_newer" not in dialog.options
        await pilot.press("a")
        await screen(pilot, ConflictScreen)
        await pilot.press("s")
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert retained.read_text() == "new" and not moved.exists()
        assert (right / "folder" / "missing").read_text() == "new"
        assert (right / "folder" / "extra").read_text() == "extra"
        assert str(left / "folder") in app._panel("left").marked


async def test_f5_only_newer_older_source_stays_marked(roots):
    left, right = roots
    source = write(left / "file", mtime=100)
    write(right / "file", "old", 300)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f5")
        await screen(pilot, ConflictScreen)
        await pilot.press("n")
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert str(source) in app._panel("left").marked
        assert (right / "file").read_text() == "old"


@pytest.mark.parametrize("folder", [False, True])
async def test_f5_identical_files_always_prompt(roots, folder):
    left, right = roots
    src_root = left / "folder" if folder else left
    dst_root = right / "folder" if folder else right
    for name in ("a", "b"):
        write(src_root / name, "new", 100)
        write(dst_root / name, "old", 100)
    if not folder:
        write(src_root / "c", "new", 100)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        marked = [str(left / "folder")] if folder else [str(left / name) for name in ("a", "b", "c")]
        app._panel("left").marked.update(marked)
        await pilot.press("f5")
        dialog = await screen(pilot, ConflictScreen)
        title = str(dialog.query_one("#conflict-title").render())
        summary = str(dialog.query_one("#conflict-summary").render())
        assert "2 identical: same size and date" in title
        assert summary.count("— identical") == 2 and "newer:" not in summary
        assert dialog.focused.id == "conflict-skip"
        await pilot.press("s")
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert all((dst_root / name).read_text() == "old" for name in ("a", "b"))
        if not folder:
            assert (right / "c").read_text() == "new"
            assert app._panel("left").marked == {str(left / "a"), str(left / "b")}
        else:
            assert str(src_root) in app._panel("left").marked


async def test_f6_identical_overwrite_moves_source(roots, isolated_trash):
    left, right = roots
    source = write(left / "file", "new", 100)
    write(right / "file", "old", 100)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f6")
        dialog = await screen(pilot, ConflictScreen)
        assert dialog.report_data.conflicts[0].identical
        await pilot.press("o")
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert not source.exists() and not app._panel("left").marked
        assert (right / "file").read_text() == "new"


async def test_ask_each_includes_identical(roots):
    left, right = roots
    source = write(left / "file", "new", 100)
    write(right / "file", "old", 100)
    app = CommanderApp(str(left), str(right))
    async with app.run_test(size=(140, 40)) as pilot:
        await main(pilot)
        await pilot.press("down", "space", "f5")
        await screen(pilot, ConflictScreen)
        await pilot.press("a")
        dialog = await screen(pilot, ConflictScreen)
        assert dialog.rel_path == "file" and "— identical" in str(
            dialog.query_one("#conflict-summary").render()
        )
        await pilot.press("r")
        from commandertui.main_screen import MainScreen

        await screen(pilot, MainScreen)
        assert (right / "file_1").read_text() == "new"
        assert (right / "file").read_text() == "old"
        assert source.exists()
