import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from textual.app import App, ComposeResult

from commandertui import preview
from commandertui.modals import MessageScreen
from commandertui.widgets import FilePanel, FunctionBar


@pytest.fixture(autouse=True)
def _cleanup_preview():
    yield
    preview.close_preview()


def test_is_macos():
    with patch("sys.platform", "darwin"):
        assert preview.is_macos() is True
    with patch("sys.platform", "linux"):
        assert preview.is_macos() is False


def test_function_bar_keys_darwin_vs_linux():
    with patch("sys.platform", "darwin"):
        keys_darwin = FunctionBar.get_keys()
        assert any(k[0] == "v" and k[1] == "Preview" for k in keys_darwin)
        width_darwin = FunctionBar.needed_width()

    with patch("sys.platform", "linux"):
        keys_linux = FunctionBar.get_keys()
        assert not any(k[0] == "v" for k in keys_linux)
        width_linux = FunctionBar.needed_width()

    assert width_darwin > width_linux


def test_toggle_preview_non_macos(tmp_path):
    test_file = tmp_path / "doc.txt"
    test_file.write_text("hello")

    with patch("sys.platform", "linux"):
        assert preview.toggle_preview(str(test_file)) is False
        assert preview.is_active() is False
        assert preview.current_path() is None


def test_toggle_preview_directory_fails(tmp_path):
    with patch("sys.platform", "darwin"):
        assert preview.toggle_preview(str(tmp_path)) is False
        assert preview.is_active() is False


def test_toggle_preview_lifecycle(tmp_path):
    file1 = tmp_path / "one.txt"
    file1.write_text("content 1")
    file2 = tmp_path / "two.txt"
    file2.write_text("content 2")

    mock_proc1 = MagicMock(spec=subprocess.Popen)
    mock_proc1.poll.return_value = None

    mock_proc2 = MagicMock(spec=subprocess.Popen)
    mock_proc2.poll.return_value = None

    with (
        patch("sys.platform", "darwin"),
        patch.object(preview, "_bring_to_front"),
        patch("subprocess.Popen", side_effect=[mock_proc1, mock_proc2]) as mock_popen,
    ):
        # 1. Open preview for file1
        started = preview.toggle_preview(str(file1))
        assert started is True
        assert preview.is_active() is True
        assert preview.current_path() == str(file1)
        mock_popen.assert_called_once_with(
            ["qlmanage", "-p", str(file1)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 2. Press v again on file1 -> closes preview
        started = preview.toggle_preview(str(file1))
        assert started is False
        assert preview.is_active() is False
        assert preview.current_path() is None
        mock_proc1.terminate.assert_called_once()


def test_toggle_preview_reopens_after_external_close(tmp_path):
    file1 = tmp_path / "one.txt"
    file1.write_text("content 1")

    mock_proc1 = MagicMock(spec=subprocess.Popen)
    mock_proc1.poll.return_value = None

    mock_proc2 = MagicMock(spec=subprocess.Popen)
    mock_proc2.poll.return_value = None

    with (
        patch("sys.platform", "darwin"),
        patch.object(preview, "_bring_to_front"),
        patch("subprocess.Popen", side_effect=[mock_proc1, mock_proc2]),
    ):
        # Open preview
        assert preview.toggle_preview(str(file1)) is True

        # External close (user pressed Esc/Space in Quick Look window)
        mock_proc1.poll.return_value = 0

        # is_active detects termination
        assert preview.is_active() is False

        # Next toggle opens a new preview
        assert preview.toggle_preview(str(file1)) is True
        assert preview.is_active() is True


def test_toggle_preview_switches_file(tmp_path):
    file1 = tmp_path / "one.txt"
    file1.write_text("content 1")
    file2 = tmp_path / "two.txt"
    file2.write_text("content 2")

    mock_proc1 = MagicMock(spec=subprocess.Popen)
    mock_proc1.poll.return_value = None

    mock_proc2 = MagicMock(spec=subprocess.Popen)
    mock_proc2.poll.return_value = None

    with (
        patch("sys.platform", "darwin"),
        patch.object(preview, "_bring_to_front"),
        patch("subprocess.Popen", side_effect=[mock_proc1, mock_proc2]),
    ):
        # Open file 1
        assert preview.toggle_preview(str(file1)) is True
        assert preview.current_path() == str(file1)

        # Open file 2 -> closes file 1 and opens file 2
        assert preview.toggle_preview(str(file2)) is True
        mock_proc1.terminate.assert_called_once()
        assert preview.current_path() == str(file2)


def test_close_preview_handles_exceptions():
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None
    mock_proc.terminate.side_effect = OSError("boom")

    preview._current_process = mock_proc
    preview._current_path = "/fake/path"

    preview.close_preview()
    assert preview._current_process is None
    assert preview._current_path is None


def test_bring_to_front_success(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("content")
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None

    with (
        patch("sys.platform", "darwin"),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        started = preview.toggle_preview(str(f))
        assert started is True
        preview._bring_to_front_worker(mock_proc)
        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "osascript"
        assert "frontmost" in cmd[2]
        assert "qlmanage" in cmd[2]


def test_bring_to_front_failure_silently_ignored(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("content")
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None

    with (
        patch("sys.platform", "darwin"),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 3.0)),
    ):
        preview._current_process = mock_proc
        preview._current_path = str(f)
        preview._bring_to_front_worker(mock_proc)
        assert preview.is_active() is True


def test_bring_to_front_already_closed_does_not_call_osascript():
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = 0  # Process already exited

    with patch("subprocess.run") as mock_run:
        preview._bring_to_front_worker(mock_proc)
        mock_run.assert_not_called()


def test_bring_to_front_cancelled_if_different_process():
    old_proc = MagicMock(spec=subprocess.Popen)
    old_proc.poll.return_value = None
    new_proc = MagicMock(spec=subprocess.Popen)
    new_proc.poll.return_value = None

    preview._current_process = new_proc

    with patch("subprocess.run") as mock_run:
        preview._bring_to_front_worker(old_proc)
        mock_run.assert_not_called()


async def _skip_boot(pilot):
    await pilot.pause()
    for _ in range(4):
        await pilot.press("enter")
        await pilot.pause()
        if pilot.app.screen.__class__.__name__ != "BootScreen":
            break


@pytest.mark.asyncio
async def test_main_screen_preview_action_on_folder_shows_message(tmp_path):
    (tmp_path / "subfolder").mkdir()
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other))
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        with patch.object(preview, "is_macos", return_value=True):
            # Row 0 is ".."
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, MessageScreen)
            assert "Preview works on files only" in app.screen.text
            await pilot.press("enter")
            await pilot.pause()

            # Move down to subfolder
            await pilot.press("down")
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, MessageScreen)
            assert "Preview works on files only" in app.screen.text


@pytest.mark.asyncio
async def test_main_screen_preview_action_on_file(tmp_path):
    doc = tmp_path / "file.txt"
    doc.write_text("hello")
    other = tmp_path.parent / "other_pane"
    other.mkdir(exist_ok=True)

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other))
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        # Move cursor from ".." to file.txt
        await pilot.press("down")

        with (
            patch.object(preview, "is_macos", return_value=True),
            patch.object(preview, "toggle_preview") as mock_toggle,
        ):
            await pilot.press("v")
            await pilot.pause()
            mock_toggle.assert_called_once_with(str(doc))
            assert not isinstance(app.screen, MessageScreen)


@pytest.mark.asyncio
async def test_main_screen_preview_action_ignored_on_linux(tmp_path):
    doc = tmp_path / "file.txt"
    doc.write_text("hello")
    other = tmp_path.parent / "other_pane"
    other.mkdir(exist_ok=True)

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other))
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("down")

        with (
            patch.object(preview, "is_macos", return_value=False),
            patch.object(preview, "toggle_preview") as mock_toggle,
        ):
            await pilot.press("v")
            await pilot.pause()
            mock_toggle.assert_not_called()
            assert not isinstance(app.screen, MessageScreen)
