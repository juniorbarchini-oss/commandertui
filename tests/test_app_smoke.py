import sys
import pytest

from commandertui.app import CommanderApp
from commandertui.modals import ConfirmScreen, MessageScreen


async def _skip_boot(pilot):
    """Boot screen no longer auto-dismisses: Enter fast-forwards the reveal,
    a later Enter dismisses it (one more if the update check finishes in
    between) -- mirrors what a user actually does."""
    await pilot.pause()
    for _ in range(4):
        await pilot.press("enter")
        await pilot.pause()
        if app_screen_is_main(pilot.app):
            break


def app_screen_is_main(app) -> bool:
    return app.screen.__class__.__name__ != "BootScreen"


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
async def test_main_bars_fit_in_120_columns(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(120, 30)) as pilot:
        await _skip_boot(pilot)
        buttons = list(app.screen.query("FnButton"))
        expected_count = 18 if sys.platform == "darwin" else 17
        assert len(buttons) == expected_count
        assert all(b.region.right <= 120 for b in buttons)


@pytest.mark.asyncio
async def test_bars_share_one_row_when_wide_and_stack_when_narrow(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    wide_width = 230 if sys.platform == "darwin" else 210
    async with app.run_test(size=(wide_width, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.pause()
        rows = {b.region.y for b in app.screen.query("FnButton")}
        assert len(rows) == 1  # everything on one line
        await pilot.resize_terminal(120, 30)
        await pilot.pause()
        rows = {b.region.y for b in app.screen.query("FnButton")}
        assert len(rows) == 2  # tools above, F-keys below
        assert all(b.region.right <= 120 for b in app.screen.query("FnButton"))


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [120, 130, 167, 168, 200, 250])
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
        assert app.update_info is not None  # looked up while the boot animation plays...
        assert app.screen.__class__.__name__ == "BootScreen"
        assert not isinstance(app.screen, UpdateScreen)  # ...but the dialog waits for the main screen
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
async def test_u_checks_now_and_says_up_to_date(tmp_path, monkeypatch):
    from commandertui import updater

    monkeypatch.setattr(updater, "check_now", lambda: (None, True))
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("u")
        await pilot.pause(0.3)
        assert any("up to date" in n.message for n in app._notifications)


@pytest.mark.asyncio
async def test_u_checks_now_and_offers_a_new_version_ignoring_the_daily_cache(tmp_path, monkeypatch):
    from commandertui import updater
    from commandertui.modals import UpdateScreen

    info = updater.UpdateInfo(current="1.2.0", latest="9.9.9", install="pip")
    monkeypatch.setattr(updater, "check_now", lambda: (info, True))
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.click("#fb-6")  # the Updates button
        await pilot.pause(0.3)
        assert isinstance(app.screen, UpdateScreen)


@pytest.mark.asyncio
async def test_u_says_when_pypi_cannot_be_reached(tmp_path, monkeypatch):
    from commandertui import updater

    monkeypatch.setattr(updater, "check_now", lambda: (None, False))
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("u")
        await pilot.pause(0.3)
        assert any("Could not reach" in n.message for n in app._notifications)


def test_check_now_ignores_cache_and_skip(monkeypatch):
    from commandertui import updater

    updater._save_cache({"checked": 9e12, "latest": "1.0.0", "skipped": "2.0.0"})
    monkeypatch.setattr(updater, "fetch_latest", lambda: "2.0.0")
    monkeypatch.setattr(updater, "detect_install", lambda: "pip")
    info, reachable = updater.check_now(current="1.0.0")
    assert reachable and info.latest == "2.0.0"  # the skipped version is offered again
    monkeypatch.setattr(updater, "fetch_latest", lambda: None)
    assert updater.check_now(current="1.0.0") == (None, False)
    monkeypatch.setattr(updater, "fetch_latest", lambda: "1.0.0")
    assert updater.check_now(current="1.0.0") == (None, True)


@pytest.mark.asyncio
async def test_f4_opens_the_editor_on_the_selected_file(tmp_path, monkeypatch):
    import contextlib
    from commandertui import main_screen

    app, left, right = await _open_confirm(tmp_path, "")
    calls = []
    monkeypatch.setattr(main_screen.editor, "editor_command", lambda: ["myeditor", "-x"])
    monkeypatch.setattr(main_screen.subprocess, "run", lambda cmd: calls.append(cmd))
    app.suspend = contextlib.nullcontext  # headless tests have no real terminal to hand over
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("down", "f4")  # row 0 is ".."; a.txt is next
        await pilot.pause()
    assert calls == [["myeditor", "-x", str(left / "a.txt")]]


@pytest.mark.asyncio
async def test_f4_on_a_folder_or_parent_does_not_start_the_editor(tmp_path, monkeypatch):
    import contextlib
    from commandertui import main_screen

    app, left, right = await _open_confirm(tmp_path, "")
    (left / "sub").mkdir()
    calls = []
    monkeypatch.setattr(main_screen.editor, "editor_command", lambda: ["myeditor"])
    monkeypatch.setattr(main_screen.subprocess, "run", lambda cmd: calls.append(cmd))
    app.suspend = contextlib.nullcontext
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("f4")  # on ".."
        await pilot.pause()
        await pilot.press("down", "f4")  # folders sort first: "sub"
        await pilot.pause()
        assert isinstance(app.screen, MessageScreen)  # tells the user it is a folder
    assert calls == []


def test_editor_command_prefers_visual_then_editor_then_fallback(monkeypatch):
    from commandertui import editor

    monkeypatch.setattr(editor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("VISUAL", "code -w")
    monkeypatch.setenv("EDITOR", "nano")
    assert editor.editor_command() == ["code", "-w"]
    monkeypatch.delenv("VISUAL")
    assert editor.editor_command() == ["nano"]
    monkeypatch.delenv("EDITOR")
    assert editor.editor_command() == ["nano"]  # first fallback
    monkeypatch.setattr(editor.shutil, "which", lambda name: None)
    assert editor.editor_command() is None
    monkeypatch.setenv("EDITOR", "does-not-exist")
    assert editor.editor_command() is None


def test_compare_side_info_and_status_text():
    from commandertui.compare_screen import side_info, status_text
    from commandertui.models import DiffEntry, DiffStatus, EntryKind, FileEntry

    older = FileEntry("a.txt", "a.txt", EntryKind.FILE, 2048, 1_700_000_000)
    newer = FileEntry("a.txt", "a.txt", EntryKind.FILE, 4096, 1_700_100_000)
    folder = FileEntry("d", "d", EntryKind.DIR, 0, 1_700_000_000)
    assert side_info(None) == "-"
    assert side_info(folder).startswith("<dir>")
    assert side_info(older).startswith("2.0 KB  20")
    left_newer = DiffEntry("a.txt", "a.txt", DiffStatus.MODIFIED, newer, older)
    right_newer = DiffEntry("a.txt", "a.txt", DiffStatus.MODIFIED, older, newer)
    only_left = DiffEntry("a.txt", "a.txt", DiffStatus.LEFT_ONLY, older, None)
    assert status_text(left_newer) == "! modified (left newer)"
    assert status_text(right_newer) == "! modified (right newer)"
    assert status_text(only_left) == "< only left"


@pytest.mark.asyncio
async def test_compare_table_shows_size_and_date_of_both_sides(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    (right / "a.txt").write_text("hello world, longer")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("c")
        await pilot.pause()
        table = app.screen.query_one("#compare-table")
        assert [str(c.label) for c in table.columns.values()][-2:] == [
            "Left (size, date)",
            "Right (size, date)",
        ]
        row = table.get_row_at(0)
        assert "5.0 B" in row[3]
        assert "19.0 B" in row[4]


@pytest.mark.asyncio
async def test_compare_shows_spinner_while_scanning_and_esc_cancels(tmp_path, monkeypatch):
    import threading
    from commandertui import compare_screen

    release = threading.Event()

    def slow_compare(left, right, cancel_check=None):
        while not release.is_set() and not (cancel_check and cancel_check()):
            release.wait(0.02)
        return []

    monkeypatch.setattr(compare_screen, "compare", slow_compare)
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("c")
        await pilot.pause(0.3)
        screen = app.screen
        assert screen.__class__.__name__ == "CompareScreen" and screen.scanning
        text = str(screen.query_one("#compare-summary").render())
        assert "Comparing" in text and "Esc to cancel" in text
        await pilot.press("m")  # actions are refused while scanning
        await pilot.pause()
        assert app.screen is screen
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"
        release.set()


@pytest.mark.asyncio
async def test_compare_spinner_stops_and_result_appears_when_done(tmp_path):
    app, left, right = await _open_confirm(tmp_path, "")
    (right / "a.txt").write_text("different")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("c")
        for _ in range(50):
            await pilot.pause(0.05)
            if not app.screen.scanning:
                break
        assert not app.screen.scanning
        assert "1 differences" in str(app.screen.query_one("#compare-summary").render())


@pytest.mark.asyncio
async def test_default_theme_is_phosphor_green_and_t_button_opens_picker(tmp_path):
    from commandertui.modals import ChoiceScreen

    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        assert app.theme == "phosphor-green"
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)


@pytest.mark.asyncio
async def test_picking_a_theme_applies_and_saves_it(tmp_path):
    from commandertui import settings

    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("t", "down", "down", "enter")  # green -> amber -> nord
        await pilot.pause()
        assert app.theme == "nord"
    assert settings.load_theme() == "nord"


@pytest.mark.asyncio
async def test_escape_in_theme_picker_restores_the_previous_theme(tmp_path):
    from commandertui import settings

    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("t", "down", "down")
        await pilot.pause()
        assert app.theme == "nord"  # previewed live while moving
        await pilot.press("escape")
        await pilot.pause()
        assert app.theme == "phosphor-green"
    assert settings.load_theme() == "phosphor-green"  # the preview was not kept


@pytest.mark.asyncio
async def test_saved_theme_is_used_at_startup_and_unknown_one_is_ignored(tmp_path):
    from commandertui import settings

    from commandertui.app import CommanderApp

    settings.save_theme("dracula")
    assert CommanderApp(str(tmp_path), str(tmp_path)).theme == "dracula"
    settings.save_theme("no-such-theme")
    assert CommanderApp(str(tmp_path), str(tmp_path)).theme == "phosphor-green"


def test_title_doubles_when_scaled():
    from commandertui.ascii_font import banner

    small, big = banner("COMMANDER").split("\n"), banner("COMMANDER", scale=2).split("\n")
    assert len(small) == 5 and len(big) == 10
    assert len(big[0]) == 2 * len(small[0])
    assert big[0] == big[1]  # each row repeated


@pytest.mark.asyncio
async def test_boot_shows_checking_updates_line_with_the_result(tmp_path, monkeypatch):
    _enable_fake_update(monkeypatch)
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.6)
        await pilot.press("enter")  # fast-forward the reveal
        await pilot.pause(0.3)
        text = str(app.screen.query_one("#boot-text").render())
        assert "CHECKING UPDATES" in text and "NEW 9.9.9" in text
        assert "READY." in text


@pytest.mark.asyncio
@pytest.mark.parametrize("width, scale", [(140, 2), (100, 1)])
async def test_boot_title_is_double_size_only_in_a_wide_window(tmp_path, width, scale):
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(width, 40)) as pilot:
        await pilot.pause()
        assert app.screen._scale == scale
        await pilot.press("enter")
        await pilot.pause(0.2)
        lines = str(app.screen.query_one("#boot-text").render()).splitlines()
        assert lines[0].startswith("+") and len(lines[0]) <= width  # the box always fits


@pytest.mark.asyncio
async def test_macos_add_network_sftp_shows_unsupported_message(tmp_path, monkeypatch):
    from commandertui import netlocations
    from commandertui.modals import InputScreen

    monkeypatch.setattr(netlocations.sys, "platform", "darwin")
    monkeypatch.setattr(netlocations.shutil, "which", lambda name: f"/usr/bin/{name}")
    app, left, right = await _open_confirm(tmp_path, "")
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("down", "enter")  # SMB -> SFTP
        await pilot.pause()
        assert isinstance(app.screen, MessageScreen)
        assert "not supported on macOS" in app.screen.text
        await pilot.press("enter")  # dismiss message
        await pilot.pause()
        await pilot.press("p", "a", "enter")  # Places -> Add -> SMB
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        assert app.screen.prompt == "SMB server (IP or name):"
        app.screen.query_one("#input-field").value = "10.0.0.5"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        assert app.screen.prompt == "SMB user:"


@pytest.mark.asyncio
async def test_places_unmount_share_checks_type_evicts_panel_and_reports_busy(tmp_path, monkeypatch):
    import os
    from commandertui import bookmarks, modals
    from commandertui.bookmarks import Place

    mount_dir = tmp_path / "MediaMount"
    sub_dir = mount_dir / "Movies"
    sub_dir.mkdir(parents=True)
    home_dir = tmp_path / "fake_home"
    home_dir.mkdir()

    fake_places = [
        Place(label="Home", path=str(home_dir), kind="home"),
        Place(label="USB", path="/Volumes/USBDISK", kind="mounted"),
        Place(label="Media @ nas (smb)", path="smb://user@nas/Media/", kind="net-saved"),
        Place(label="HD1", path="/Volumes/HD1", kind="mounted"),
    ]
    monkeypatch.setattr(modals, "all_places", lambda: list(fake_places))
    monkeypatch.setattr(bookmarks.os.path, "expanduser", lambda p: str(home_dir) if p == "~" else p)
    monkeypatch.setattr(
        modals,
        "local_path",
        lambda uri: str(mount_dir) if uri == "smb://user@nas/Media/" else None,
    )
    monkeypatch.setattr(
        modals,
        "is_smb_mount_point",
        lambda path: path == "/Volumes/HD1",
    )

    unmounted = []
    busy = {"flag": False}

    def fake_unmount(target):
        unmounted.append(target)
        if busy["flag"]:
            return False, "Volume Media on disk4 failed to unmount: dissented by PID 123"
        return True, ""

    monkeypatch.setattr(modals, "unmount", fake_unmount)

    app = CommanderApp(left_path=str(sub_dir), right_path=str(home_dir))
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        assert app._panel("left").path == str(sub_dir)
        await pilot.press("p")
        await pilot.pause()

        # Row 0 is Home (non-network) -> pressing 'e' shows "Not a mounted network location"
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, MessageScreen)
        assert "Not a mounted network location" in app.screen.text
        await pilot.press("enter")
        await pilot.pause()

        # Row 1 is USB (mounted drive, NOT smbfs) -> also refused
        await pilot.press("down", "e")
        await pilot.pause()
        assert isinstance(app.screen, MessageScreen)
        assert "Not a mounted network location" in app.screen.text
        await pilot.press("enter")
        await pilot.pause()

        # Row 2 is mounted SMB URI location -> moves left panel to home_dir and unmounts
        await pilot.press("down", "e")
        await pilot.pause()
        assert unmounted == ["smb://user@nas/Media/"]
        assert app._panel("left").path == str(home_dir)

        # Row 3 is mounted smbfs volume (/Volumes/HD1) from Finder/mount -> unmounts
        app._panel("left").path = "/Volumes/HD1/docs"
        await pilot.press("down", "e")
        await pilot.pause()
        assert unmounted[-1] == "/Volumes/HD1"
        assert app._panel("left").path == str(home_dir)

        # If unmount fails (busy), shows error in MessageScreen
        busy["flag"] = True
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, MessageScreen)
        assert "failed to unmount" in app.screen.text
