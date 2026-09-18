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
