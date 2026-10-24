import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from commandertui.widgets import FilePanel


class _Harness(App):
    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        yield FilePanel(self.path, side="left", id="panel-left")


@pytest.mark.asyncio
async def test_marking_does_not_reset_cursor_to_top(tmp_path):
    """Regression: marking used to call refresh_listing(), which rebuilds
    the DataTable and resets its cursor to row 0 -- so marking a second item
    always meant scrolling back down from the top again."""
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("x")

    app = _Harness(str(tmp_path))
    async with app.run_test() as pilot:
        panel = app.query_one(FilePanel)
        table = panel.query_one(DataTable)
        # Move to the 3rd real entry (row 0 is "..").
        table.move_cursor(row=3)
        row_before = table.cursor_coordinate.row

        panel.toggle_mark_selected()

        assert table.cursor_coordinate.row == row_before
        assert len(panel.marked) == 1


def test_free_space_label_for_a_real_directory(tmp_path):
    from commandertui.widgets import _free_space_label

    label = _free_space_label(str(tmp_path))
    assert label.endswith(" free") and label.split()[1] in ("B", "KB", "MB", "GB", "TB")


def test_free_space_label_is_empty_when_path_is_gone(tmp_path):
    from commandertui.widgets import _free_space_label

    assert _free_space_label(str(tmp_path / "missing")) == ""


@pytest.mark.asyncio
async def test_sorting_columns_and_header_navigation(tmp_path, monkeypatch):
    import os
    import time
    from commandertui import settings
    from commandertui.app import CommanderApp

    # Setup files with distinct names, sizes, and mtimes
    (tmp_path / "z_dir").mkdir()
    (tmp_path / "a_dir").mkdir()

    f1 = tmp_path / "small_new.txt"
    f1.write_bytes(b"x" * 10)

    f2 = tmp_path / "big_old.txt"
    f2.write_bytes(b"x" * 1000)

    now = time.time()
    os.utime(f1, (now, now))
    os.utime(f2, (now - 10000, now - 10000))

    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))

    app = CommanderApp(left_path=str(tmp_path), right_path=str(tmp_path))
    async with app.run_test(size=(140, 30)) as pilot:
        await pilot.pause()
        for _ in range(4):
            await pilot.press("enter")
            await pilot.pause()
            if app.screen.__class__.__name__ != "BootScreen":
                break

        panel = app._panel("left")
        table = panel.query_one(DataTable)

        # Initial default sort: Name ascending (folders first A-Z, then files A-Z)
        # rows: "..", "a_dir/", "z_dir/", "big_old.txt", "small_new.txt"
        names = [panel._rows[i][0] for i in range(len(panel._rows))]
        assert names == ["..", "a_dir", "z_dir", "big_old.txt", "small_new.txt"]
        assert panel.sort_column == "Name" and not panel.sort_desc

        # Navigate UP beyond row 0 to enter header mode
        table.move_cursor(row=0)
        await pilot.press("up")
        await pilot.pause()
        assert panel.header_active
        assert panel.header_selected_index == 0  # Name

        # Press Enter on Name -> inverts to Name descending
        await pilot.press("enter")
        await pilot.pause()
        assert panel.sort_column == "Name" and panel.sort_desc
        names = [panel._rows[i][0] for i in range(len(panel._rows))]
        assert names == ["..", "z_dir", "a_dir", "small_new.txt", "big_old.txt"]

        # Navigate RIGHT to Size column
        await pilot.press("right")
        await pilot.pause()
        assert panel.header_selected_index == 1

        # Press Enter on Size -> first Enter sorts Size descending (largest first)
        await pilot.press("enter")
        await pilot.pause()
        assert panel.sort_column == "Size" and panel.sort_desc
        names = [panel._rows[i][0] for i in range(len(panel._rows))]
        assert names == ["..", "a_dir", "z_dir", "big_old.txt", "small_new.txt"]

        # Second Enter on Size -> inverts to Size ascending (smallest first)
        await pilot.press("enter")
        await pilot.pause()
        assert panel.sort_column == "Size" and not panel.sort_desc
        names = [panel._rows[i][0] for i in range(len(panel._rows))]
        assert names == ["..", "a_dir", "z_dir", "small_new.txt", "big_old.txt"]

        # Navigate RIGHT to Modified (Date) column
        await pilot.press("right")
        await pilot.pause()
        assert panel.header_selected_index == 2

        # Press Enter on Date -> first Enter sorts Date descending (newest first)
        await pilot.press("enter")
        await pilot.pause()
        assert panel.sort_column == "Modified" and panel.sort_desc
        names = [panel._rows[i][0] for i in range(len(panel._rows))]
        assert names[3:] == ["small_new.txt", "big_old.txt"]

        # DOWN arrow exits header mode back to rows
        await pilot.press("down")
        await pilot.pause()
        assert not panel.header_active

        # Test mouse click via HeaderSelected event (click on Col 1: Name)
        table.post_message(
            DataTable.HeaderSelected(table, table.columns[list(table.columns.keys())[1]], 1, "Name")
        )
        await pilot.pause()
        assert panel.sort_column == "Name" and not panel.sort_desc

        # Test mouse click on INACTIVE panel's header:
        # Currently "left" is active. Click Size column (Col 2) on "right" panel.
        assert app.active_side == "left"
        right_panel = app._panel("right")
        right_table = right_panel.query_one(DataTable)
        right_table.post_message(
            DataTable.HeaderSelected(
                right_table, right_table.columns[list(right_table.columns.keys())[2]], 2, "Size"
            )
        )
        await pilot.pause()
        # Right panel should now be sorted by Size desc and activated
        assert app.active_side == "right"
        assert right_panel.active is True
        assert panel.active is False
        assert right_panel.sort_column == "Size" and right_panel.sort_desc
        # Left panel sort remains unchanged
        assert panel.sort_column == "Name" and not panel.sort_desc

        # Check saved setting persistence
        saved = settings.load_sort("left")
        assert saved == ("Name", False)
        saved_right = settings.load_sort("right")
        assert saved_right == ("Size", True)
