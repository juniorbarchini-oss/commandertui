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


async def _open_confirm(tmp_path, key):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "a.txt").write_text("hello")
    app = CommanderApp(left_path=str(left), right_path=str(right))
    return app, left, right


@pytest.mark.asyncio
async def test_confirm_copy_buttons_click_yes(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "f5")
    async with app.run_test(size=(120, 40)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("down", "space", "f5")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.focused.id == "yes"  # a copy starts on Yes
        await pilot.click("#yes")
        await pilot.pause()
    assert (right / "a.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_confirm_delete_starts_on_no_and_enter_cancels(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "f8")
    async with app.run_test(size=(120, 40)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("down", "space", "f8")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.focused.id == "no"  # a delete never defaults to Yes
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
    assert (left / "a.txt").exists()


@pytest.mark.asyncio
async def test_confirm_arrow_moves_between_buttons(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "f8")
    async with app.run_test(size=(120, 40)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("down", "space", "f8")
        await pilot.pause()
        await pilot.press("left")
        assert app.screen.focused.id == "yes"


@pytest.mark.asyncio
async def test_progress_cancel_button_asks_then_cancels(tmp_path):
    from commandertui.progress_screen import ProgressScreen

    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(120, 40)) as pilot:
        await _skip_boot(pilot)
        screen = ProgressScreen("Copy", 1)
        app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#cancel").display and not screen.query_one("#yes-cancel").display

        await pilot.click("#cancel")
        await pilot.pause()
        assert screen.asking and screen.query_one("#yes-cancel").display
        assert screen.focused.id == "no-continue"  # safe default

        await pilot.click("#no-continue")
        await pilot.pause()
        assert not screen.asking and not screen.cancel_requested and screen.query_one("#cancel").display

        await pilot.click("#cancel")
        await pilot.pause()
        await pilot.click("#yes-cancel")
        await pilot.pause()
        assert screen.cancel_requested


@pytest.mark.asyncio
async def test_active_panel_is_marked_and_follows_tab(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(130, 30)) as pilot:
        await _skip_boot(pilot)
        assert app._panel("left").has_class("active-panel")
        assert not app._panel("right").has_class("active-panel")
        await pilot.press("tab")
        assert app._panel("right").has_class("active-panel")
        assert not app._panel("left").has_class("active-panel")


@pytest.mark.asyncio
async def test_function_bar_click_runs_the_action(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(130, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("down", "space")
        copy_button = next(b for b in app.screen.query("FnButton") if b.action_name == "copy")
        await pilot.click(f"#{copy_button.id}")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)


@pytest.mark.asyncio
async def test_copy_with_nothing_selected_says_so(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(130, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("tab", "f5")  # right pane is empty
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert any("Nothing selected" in n.message for n in app._notifications)


async def _rename_via_dialog(app, pilot, new_name):
    await _skip_boot(pilot)
    await pilot.press("down", "f2")  # row 0 is ".."; a.txt is next
    await pilot.pause()
    field = app.screen.query_one("#input-field")
    assert field.value == "a.txt"  # current name preloaded
    field.value = new_name
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_rename_with_f2(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _rename_via_dialog(app, pilot, "b.txt")
        assert (left / "b.txt").read_text() == "hello" and not (left / "a.txt").exists()
        assert app._panel("left").selected_name() == "b.txt"  # cursor follows


@pytest.mark.asyncio
async def test_rename_never_overwrites_existing(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    (left / "b.txt").write_text("other")
    async with app.run_test(size=(140, 30)) as pilot:
        await _rename_via_dialog(app, pilot, "b.txt")
        assert (left / "a.txt").read_text() == "hello"
        assert (left / "b.txt").read_text() == "other"


@pytest.mark.asyncio
async def test_rename_rejects_path_separators(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _rename_via_dialog(app, pilot, "../evil")
        assert (left / "a.txt").exists() and not (tmp_path / "evil").exists()


@pytest.mark.asyncio
async def test_mkdir_dialog_ok_button_creates_folder(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("f7")
        await pilot.pause()
        app.screen.query_one("#input-field").value = "newdir"
        await pilot.click("#ok")
        await pilot.pause()
    assert (left / "newdir").is_dir()


@pytest.mark.asyncio
async def test_mkdir_dialog_cancel_button_creates_nothing(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("f7")
        await pilot.pause()
        app.screen.query_one("#input-field").value = "newdir"
        await pilot.click("#cancel")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen) and app.screen.__class__.__name__ == "MainScreen"
    assert not (left / "newdir").exists()


@pytest.mark.asyncio
async def test_message_ok_button_closes(tmp_path):
    from commandertui.modals import MessageScreen

    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        app.push_screen(MessageScreen("Cancelled."))
        await pilot.pause()
        await pilot.click("#ok")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.asyncio
async def test_size_close_button_closes(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("down", "i")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "SizeScreen"
        await pilot.click("#close")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.asyncio
async def test_compare_bar_back_button(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "CompareScreen"
        await pilot.click("#fn-0")  # Esc Back
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.asyncio
async def test_main_bars_fit_in_100_columns(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(100, 30)) as pilot:
        await _skip_boot(pilot)
        buttons = list(app.screen.query("FnButton"))
        assert len(buttons) == 12
        assert all(b.region.right <= 100 for b in buttons)


@pytest.mark.asyncio
async def test_bars_share_one_row_when_wide_and_stack_when_narrow(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(200, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.pause()
        rows = {b.region.y for b in app.screen.query("FnButton")}
        assert len(rows) == 1  # everything on one line
        await pilot.resize_terminal(100, 30)
        await pilot.pause()
        rows = {b.region.y for b in app.screen.query("FnButton")}
        assert len(rows) == 2  # tools above, F-keys below
        assert all(b.region.right <= 100 for b in app.screen.query("FnButton"))


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [90, 120, 143, 144, 160, 250])
async def test_bars_never_overflow_at_any_width(tmp_path, width):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(width, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.pause()
        assert all(b.region.right <= width for b in app.screen.query("FnButton"))


@pytest.mark.asyncio
async def test_selection_counter_counts_files_and_folder_sizes(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    (left / "a.txt").write_text("x" * 1000)
    (left / "dir").mkdir()
    (left / "dir" / "big.bin").write_bytes(b"y" * 5000)
    async with app.run_test(size=(150, 30)) as pilot:
        await _skip_boot(pilot)
        label = app._panel("left").query_one("#left-sel")
        assert str(label.render()).strip() == ""
        # rows: "..", dir/, a.txt (folders sort first)
        await pilot.press("down", "space", "down", "space")
        await pilot.pause(0.5)
        text = str(label.render())
        assert "2 marked" in text and "5.9 KB" in text
        await pilot.press("space")  # unmark a.txt
        await pilot.pause(0.5)
        assert "1 marked" in str(label.render()) and "4.9 KB" in str(label.render())


@pytest.mark.asyncio
async def test_selection_counter_clears_after_the_marks_are_gone(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(150, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("down", "space")
        await pilot.pause()
        label = app._panel("left").query_one("#left-sel")
        assert "1 marked" in str(label.render())
        await pilot.press("r")  # refresh keeps marks; navigating clears them
        app._panel("left").marked.clear()
        app._panel("left").refresh_listing()
        await pilot.pause()
        assert str(label.render()).strip() == ""
