import pytest

from commandertui.app import CommanderApp
from commandertui.modals import ConfirmScreen


async def _skip_boot(pilot):
    """Boot screen no longer auto-dismisses: Enter fast-forwards the reveal,
    a second Enter dismisses it -- mirrors what a user actually does."""
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_app_boots_and_dismisses_boot_screen(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "a.txt").write_text("hello")

    app = CommanderApp(left_path=str(left), right_path=str(right))
    async with app.run_test() as pilot:
        await _skip_boot(pilot)
        from commandertui.main_screen import MainScreen

        assert isinstance(app.screen, MainScreen)


@pytest.mark.asyncio
async def test_tab_switches_active_pane(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()

    app = CommanderApp(left_path=str(left), right_path=str(right))
    async with app.run_test() as pilot:
        await _skip_boot(pilot)
        assert app.active_side == "left"
        await pilot.press("tab")
        assert app.active_side == "right"


@pytest.mark.asyncio
async def test_mark_and_copy_via_confirm(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "a.txt").write_text("hello")

    app = CommanderApp(left_path=str(left), right_path=str(right))
    async with app.run_test() as pilot:
        await _skip_boot(pilot)
        await pilot.press("down")  # row 0 is ".."; move onto a.txt
        await pilot.press("space")  # mark a.txt
        await pilot.press("f5")  # copy -> pushes ConfirmScreen
        await pilot.pause()
        await pilot.press("y")  # confirm
        await pilot.pause()

    assert (right / "a.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_confirm_dialog_shows_real_size_not_zero(tmp_path):
    """Regression: manually-marked items never got a `size` on their
    QueuedOp, so the confirm dialog always read "0.0 B total" no matter how
    big the file actually was."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "a.txt").write_text("x" * 5000)

    app = CommanderApp(left_path=str(left), right_path=str(right))
    async with app.run_test() as pilot:
        await _skip_boot(pilot)
        await pilot.press("down")
        await pilot.press("space")
        await pilot.press("f5")
        await pilot.pause()

        confirm = app.screen
        assert isinstance(confirm, ConfirmScreen)
        assert confirm.queue.summary()["total_bytes"] == 5000


@pytest.mark.asyncio
async def test_enter_opens_directory(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "sub").mkdir(parents=True)
    right.mkdir()

    app = CommanderApp(left_path=str(left), right_path=str(right))
    async with app.run_test() as pilot:
        await _skip_boot(pilot)
        await pilot.press("down")  # onto "sub/"
        await pilot.press("enter")
        await pilot.pause()

        panel = app._panel("left")
        assert panel.path == str(left / "sub")


@pytest.mark.asyncio
async def test_tab_moves_real_focus_to_the_other_pane(tmp_path):
    """Regression: switching the active pane must move Textual's actual
    focus to that pane's DataTable, or arrow-key navigation keeps acting on
    whichever pane was focused first regardless of which one looks active.
    """
    from textual.widgets import DataTable

    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()

    app = CommanderApp(left_path=str(left), right_path=str(right))
    async with app.run_test() as pilot:
        await _skip_boot(pilot)
        await pilot.press("tab")
        assert app.active_side == "right"
        assert app.focused is app._panel("right").query_one(DataTable)
