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


async def test_ask_each_apply_remaining(roots):
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
