"""Enter on a file, go to path, find by name, and the help screen."""

import os
import time

from textual.widgets import Input, OptionList, Static

from commandertui import scanner
from commandertui.modals import FindScreen, HelpScreen, InputScreen, MessageScreen, help_text
from commandertui.preview_screen import PreviewScreen


async def _skip_boot(pilot) -> None:
    await pilot.pause()
    for _ in range(4):
        await pilot.press("enter")
        await pilot.pause()
        if pilot.app.screen.__class__.__name__ != "BootScreen":
            break


def _app(left, right=None):
    from commandertui.app import CommanderApp

    return CommanderApp(left_path=str(left), right_path=str(right or left), check_updates=False)


def _tree(tmp_path):
    root = tmp_path / "root"
    (root / "docs" / "deep").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "notes.txt").write_text("hola\n")
    (root / "docs" / "Report-2026.pdf").write_bytes(b"%PDF-1.4\n")
    (root / "docs" / "deep" / "report_old.txt").write_text("x")
    (root / ".hidden" / "report_secret.txt").write_text("x")
    return root


async def _type(pilot, text: str) -> None:
    field = pilot.app.screen.query_one(Input)
    field.value = text
    await pilot.press("enter")
    await pilot.pause()


async def _wait_find(pilot) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        await pilot.pause(0.05)
        screen = pilot.app.screen
        if not isinstance(screen, FindScreen):
            return
        status = str(screen.query_one("#find-status", Static).content)
        if not status.startswith("Searching"):
            return


# ------------------------------------------------------------------ find engine


def test_find_names_matches_substring_and_glob_case_insensitively(tmp_path):
    root = _tree(tmp_path)
    found, truncated = scanner.find_names(str(root), "report")
    assert [os.path.relpath(p, root) for p in found] == ["docs/Report-2026.pdf", "docs/deep/report_old.txt"]
    assert truncated is False
    found, _ = scanner.find_names(str(root), "*.PDF")
    assert [os.path.basename(p) for p in found] == ["Report-2026.pdf"]
    found, _ = scanner.find_names(str(root), "deep")
    assert [os.path.relpath(p, root) for p in found] == ["docs/deep"]


def test_find_names_hidden_limit_cancel_and_symlink_loops(tmp_path):
    root = _tree(tmp_path)
    found, _ = scanner.find_names(str(root), "report", show_hidden=True)
    assert any(p.endswith("report_secret.txt") for p in found)

    found, truncated = scanner.find_names(str(root), "report", limit=1)
    assert len(found) == 1 and truncated is True

    assert scanner.find_names(str(root), "report", cancelled=lambda: True) == ([], False)
    assert scanner.find_names(str(root), "   ") == ([], False)

    os.symlink(root, root / "docs" / "loop")
    found, _ = scanner.find_names(str(root), "report")
    assert len(found) == 2  # the link is not entered


# ------------------------------------------------------------------ Enter on a file


async def test_enter_on_a_file_opens_the_preview_and_on_a_folder_enters_it(tmp_path):
    root = _tree(tmp_path)
    app = _app(root)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        panel = app._panel("left")
        panel.move_cursor_to("notes.txt")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, PreviewScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert panel.selected_name() == "notes.txt"

        panel.move_cursor_to("docs")
        await pilot.press("enter")
        await pilot.pause()
        assert panel.path == str(root / "docs")
        assert app.screen.__class__.__name__ == "MainScreen"


# ------------------------------------------------------------------ go to path


async def test_go_to_path_enters_folders_and_selects_files(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    app = _app(root)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        panel = app._panel("left")

        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        assert app.screen.query_one(Input).value == str(root)
        await _type(pilot, str(root / "docs" / "deep"))
        assert panel.path == str(root / "docs" / "deep")

        await pilot.press("g")
        await pilot.pause()
        await _type(pilot, "~/docs/Report-2026.pdf")
        assert panel.path == str(root / "docs")
        assert panel.selected_name() == "Report-2026.pdf"

        await pilot.press("g")
        await pilot.pause()
        await _type(pilot, "../notes.txt")  # relative to the current folder
        assert panel.path == str(root) and panel.selected_name() == "notes.txt"

        await pilot.press("g")
        await pilot.pause()
        await _type(pilot, str(root / "nope" / "missing"))
        assert isinstance(app.screen, MessageScreen)
        assert panel.path == str(root)


async def test_go_to_a_hidden_file_reveals_hidden_files(tmp_path):
    root = _tree(tmp_path)
    (root / ".env").write_text("A=1\n")
    app = _app(root)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        panel = app._panel("left")
        await pilot.press("g")
        await pilot.pause()
        await _type(pilot, str(root / ".env"))
        assert panel.show_hidden and panel.selected_name() == ".env"


# ------------------------------------------------------------------ find dialog


async def test_find_lists_matches_and_enter_jumps_to_the_file(tmp_path):
    root = _tree(tmp_path)
    app = _app(root)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        panel = app._panel("left")
        await pilot.press("f")
        await pilot.pause()
        await _type(pilot, "report")
        assert isinstance(app.screen, FindScreen)
        await _wait_find(pilot)
        screen = app.screen
        assert [os.path.relpath(p, root) for p in screen.results] == [
            "docs/Report-2026.pdf",
            "docs/deep/report_old.txt",
        ]
        assert "2 found" in str(screen.query_one("#find-status", Static).content)
        assert screen.query_one(OptionList).option_count == 2

        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"
        assert panel.path == str(root / "docs" / "deep")
        assert panel.selected_name() == "report_old.txt"


async def test_find_with_no_matches_and_escape(tmp_path):
    root = _tree(tmp_path)
    app = _app(root)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("f")
        await pilot.pause()
        await _type(pilot, "zzz-nothing")
        await _wait_find(pilot)
        assert "Nothing matches" in str(app.screen.query_one("#find-status", Static).content)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"
        assert app._panel("left").path == str(root)


# ------------------------------------------------------------------ help


def test_help_text_lists_every_main_screen_key():
    from commandertui.main_screen import MainScreen

    text = help_text()
    names = {"question_mark": "?", "slash": "/", "plus": "+", "minus": "-", "asterisk": "*"}
    skipped = {"up", "down", "left", "right", "enter", "tab", "backspace", "space", "delete", "f1"}
    for binding in MainScreen.BINDINGS:
        if binding.key in skipped:
            continue
        key = names.get(binding.key, binding.key)
        shown = key.upper() if key.startswith("f") and key[1:].isdigit() else key
        assert any(
            shown in line.split("   ")[0].split() for line in text.splitlines() if line.startswith("  ")
        ), binding.key


async def test_help_opens_with_question_mark_and_f1_and_closes_with_escape(tmp_path):
    app = _app(_tree(tmp_path))
    async with app.run_test(size=(120, 30)) as pilot:
        await _skip_boot(pilot)
        for key in ("question_mark", "f1"):
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            assert "Go to a path" in str(app.screen.query_one("#help-text", Static).content)
            box = app.screen.query_one("#help-box")
            assert box.region.right <= 120 and box.region.bottom <= 30
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "MainScreen"
