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
async def test_main_bars_fit_in_110_columns(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(110, 30)) as pilot:
        await _skip_boot(pilot)
        buttons = list(app.screen.query("FnButton"))
        assert len(buttons) == 14
        assert all(b.region.right <= 110 for b in buttons)


@pytest.mark.asyncio
async def test_bars_share_one_row_when_wide_and_stack_when_narrow(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(200, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.pause()
        rows = {b.region.y for b in app.screen.query("FnButton")}
        assert len(rows) == 1  # everything on one line
        await pilot.resize_terminal(110, 30)
        await pilot.pause()
        rows = {b.region.y for b in app.screen.query("FnButton")}
        assert len(rows) == 2  # tools above, F-keys below
        assert all(b.region.right <= 110 for b in app.screen.query("FnButton"))


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [110, 130, 167, 168, 200, 250])
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


@pytest.mark.asyncio
async def test_selection_counter_spins_while_folders_are_counted(tmp_path, monkeypatch):
    import threading

    from commandertui import widgets

    release = threading.Event()
    monkeypatch.setattr(widgets, "path_size", lambda p: (release.wait(5), 4096)[1])
    app, left, right = await _open_confirm(tmp_path, "")
    (left / "dir").mkdir()
    async with app.run_test(size=(150, 30)) as pilot:
        await _skip_boot(pilot)
        label = app._panel("left").query_one("#left-sel")
        await pilot.press("down", "space")  # marks dir/ -> slow count starts
        seen = set()
        for _ in range(6):
            await pilot.pause(0.15)
            seen.add(str(label.render()).strip()[-1])
        assert len(seen & set(widgets.SPINNER_FRAMES)) >= 2  # the frame keeps changing
        release.set()
        await pilot.pause(0.5)
        final = str(label.render())
        assert "4.0 KB" in final and not final.rstrip().endswith(tuple(widgets.SPINNER_FRAMES))
@pytest.mark.asyncio
async def test_places_buttons_close_and_add(tmp_path):
    from commandertui.modals import ADD_NETWORK, PlacesScreen

    app, left, right = await _open_confirm(tmp_path, "")
    results = []
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        app.push_screen(PlacesScreen(), results.append)
        await pilot.pause()
        await pilot.click("#close")
        await pilot.pause()
        app.push_screen(PlacesScreen(), results.append)
        await pilot.pause()
        await pilot.click("#add")
        await pilot.pause()
    assert results == [None, ADD_NETWORK]


@pytest.mark.asyncio
async def test_choice_cancel_button(tmp_path):
    from commandertui.modals import ChoiceScreen

    app, left, right = await _open_confirm(tmp_path, "")
    results = []
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        app.push_screen(ChoiceScreen("Protocol", ["SMB", "SFTP", "FTP"]), results.append)
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause()
    assert results == [None]


@pytest.mark.asyncio
async def test_places_save_here_bookmarks_the_active_folder(tmp_path, monkeypatch):
    from commandertui import bookmarks
    from commandertui.modals import PlacesScreen

    monkeypatch.setattr(bookmarks, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(bookmarks, "BOOKMARKS_FILE", str(tmp_path / "cfg" / "bookmarks.json"))
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, PlacesScreen)
        await pilot.press("s")  # opens the name prompt, preloaded with the folder name
        await pilot.pause()
        field = app.screen.query_one("#input-field")
        assert field.value == "left"
        field.value = "My left"
        await pilot.click("#ok")
        await pilot.pause()
        assert isinstance(app.screen, PlacesScreen)
        assert any(p.label == "My left" and p.path == str(left) for p in bookmarks.load_saved())
    assert [p.label for p in bookmarks.load_saved()] == ["My left"]


def _names(app, side="left"):
    return [name for name, _ in app._panel(side)._rows]


@pytest.mark.asyncio
async def test_filter_narrows_live_and_enter_keeps_it(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    (left / "Windows11.iso").write_text("x")
    (left / "notes.md").write_text("x")
    async with app.run_test(size=(160, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("slash")
        await pilot.pause()
        app.screen.query_one("#filter-field").value = "WIN"  # case-insensitive "contains"
        await pilot.pause()
        assert _names(app) == ["..", "Windows11.iso"]  # narrowed while the box is still open
        await pilot.press("enter")
        await pilot.pause()
        assert _names(app) == ["..", "Windows11.iso"]  # Enter keeps it
        assert "filter: WIN" in str(app._panel("left").query_one("#left-pathbar").render())


@pytest.mark.asyncio
async def test_filter_escape_clears_it(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    (left / "other.txt").write_text("x")
    async with app.run_test(size=(160, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("slash")
        await pilot.pause()
        app.screen.query_one("#filter-field").value = "other"
        await pilot.pause()
        assert _names(app) == ["..", "other.txt"]
        await pilot.press("escape")
        await pilot.pause()
        assert _names(app) == ["..", "a.txt", "other.txt"]


@pytest.mark.asyncio
async def test_filter_resets_when_entering_another_folder(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    (left / "sub").mkdir()
    (left / "sub" / "inner.txt").write_text("x")
    async with app.run_test(size=(160, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("slash")
        await pilot.pause()
        app.screen.query_one("#filter-field").value = "sub"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "enter")  # into sub/
        await pilot.pause()
        assert app._panel("left").filter_text == ""
        assert _names(app) == ["..", "inner.txt"]


@pytest.mark.asyncio
async def test_filter_does_not_rescan_the_disk_while_typing(tmp_path, monkeypatch):
    import os

    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(160, 30)) as pilot:
        await _skip_boot(pilot)
        scans = []
        real = os.scandir
        monkeypatch.setattr(os, "scandir", lambda *a, **k: (scans.append(1), real(*a, **k))[1])
        await pilot.press("slash")
        await pilot.pause()
        for text in ("a", "a.", "a.t"):
            app.screen.query_one("#filter-field").value = text
            await pilot.pause()
        assert scans == []


async def _three_files(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    (left / "x.iso").write_text("x")
    (left / "Y.ISO").write_text("x")
    (left / "notes.md").write_text("x")
    return app, left, right


def _marked_names(app):
    import os

    return sorted(os.path.basename(p) for p in app._panel("left").marked)


@pytest.mark.asyncio
async def test_plus_marks_by_wildcard_ignoring_case(tmp_path):
    app, left, right = await _three_files(tmp_path)
    async with app.run_test(size=(170, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("plus")
        await pilot.pause()
        app.screen.query_one("#input-field").value = "*.iso"
        await pilot.press("enter")
        await pilot.pause()
        assert _marked_names(app) == ["Y.ISO", "x.iso"]


@pytest.mark.asyncio
async def test_plus_plain_text_means_contains(tmp_path):
    app, left, right = await _three_files(tmp_path)
    async with app.run_test(size=(170, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("plus")
        await pilot.pause()
        app.screen.query_one("#input-field").value = "note"
        await pilot.press("enter")
        await pilot.pause()
        assert _marked_names(app) == ["notes.md"]


@pytest.mark.asyncio
async def test_minus_unmarks_and_asterisk_inverts(tmp_path):
    app, left, right = await _three_files(tmp_path)
    async with app.run_test(size=(170, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("asterisk")  # nothing marked -> everything marked
        await pilot.pause()
        assert _marked_names(app) == ["Y.ISO", "a.txt", "notes.md", "x.iso"]
        await pilot.press("minus")
        await pilot.pause()
        app.screen.query_one("#input-field").value = "*.iso"
        await pilot.press("enter")
        await pilot.pause()
        assert _marked_names(app) == ["a.txt", "notes.md"]
        await pilot.press("asterisk")  # invert: only the two .iso come back
        await pilot.pause()
        assert _marked_names(app) == ["Y.ISO", "x.iso"]


@pytest.mark.asyncio
async def test_pattern_select_respects_the_filter_and_updates_counter(tmp_path):
    app, left, right = await _three_files(tmp_path)
    async with app.run_test(size=(170, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("slash")
        await pilot.pause()
        app.screen.query_one("#filter-field").value = "x.iso"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("asterisk")  # only the visible row is inverted
        await pilot.pause()
        assert _marked_names(app) == ["x.iso"]
        assert "1 marked" in str(app._panel("left").query_one("#left-sel").render())


@pytest.mark.asyncio
async def test_select_button_opens_the_menu(tmp_path):
    from commandertui.modals import ChoiceScreen

    app, left, right = await _three_files(tmp_path)
    async with app.run_test(size=(170, 30)) as pilot:
        await _skip_boot(pilot)
        button = next(b for b in app.screen.query("FnButton") if b.action_name == "select_menu")
        await pilot.click(f"#{button.id}")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)


def _enable_fake_update(monkeypatch, install="pipx", latest="9.9.9"):
    from commandertui import updater

    monkeypatch.delenv("COMMANDERTUI_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setenv("COMMANDERTUI_FAKE_LATEST", latest)
    monkeypatch.setattr(updater, "detect_install", lambda: install)


@pytest.mark.asyncio
async def test_update_dialog_appears_after_the_boot_screen(tmp_path, monkeypatch):
    from commandertui.modals import UpdateScreen

    _enable_fake_update(monkeypatch)
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await pilot.pause(0.5)
        assert app.update_info is None  # nothing is even looked up during the boot animation
        assert not isinstance(app.screen, UpdateScreen)
        await _skip_boot(pilot)
        await pilot.pause(0.5)
        assert isinstance(app.screen, UpdateScreen)
        assert "9.9.9" in app.sub_title


@pytest.mark.asyncio
async def test_update_later_keeps_the_header_hint_and_u_reopens_it(tmp_path, monkeypatch):
    from commandertui.modals import UpdateScreen

    _enable_fake_update(monkeypatch)
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.pause(0.5)
        await pilot.press("escape")  # Later
        await pilot.pause()
        assert not isinstance(app.screen, UpdateScreen)
        assert "update 9.9.9 available" in app.sub_title
        await pilot.press("u")
        await pilot.pause()
        assert isinstance(app.screen, UpdateScreen)


@pytest.mark.asyncio
async def test_update_skip_is_remembered(tmp_path, monkeypatch):
    from commandertui import updater

    _enable_fake_update(monkeypatch)
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.pause(0.5)
        await pilot.click("#skip")
        await pilot.pause()
        assert app.update_info is None and app.sub_title == ""
    assert updater._load_cache().get("skipped") == "9.9.9"


@pytest.mark.asyncio
async def test_update_now_runs_the_command_and_reports(tmp_path, monkeypatch):
    from commandertui import updater

    _enable_fake_update(monkeypatch)
    ran = []
    monkeypatch.setattr(updater, "run_update", lambda info: ran.append(info.latest) or True)
    app, left, right = await _open_confirm(tmp_path, "")
    import contextlib

    app.suspend = contextlib.nullcontext  # headless tests have no real terminal to hand over
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.pause(0.5)
        await pilot.click("#now")
        await pilot.pause()
        assert ran == ["9.9.9"]
        assert app.update_info is None
        assert app.screen.__class__.__name__ == "MessageScreen"


@pytest.mark.asyncio
async def test_source_checkout_offers_no_update_button(tmp_path, monkeypatch):
    _enable_fake_update(monkeypatch, install="source")
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.pause(0.5)
        assert not app.screen.query("#now")
        assert app.screen.query("#later")


@pytest.mark.asyncio
async def test_u_says_up_to_date_when_nothing_is_pending(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("u")
        await pilot.pause()
        assert any("up to date" in n.message for n in app._notifications)
