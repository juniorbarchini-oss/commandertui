"""Regression tests for the preview fixes that followed the v1.7.1 review."""

import os
import time
from unittest.mock import patch

import pytest
from textual.widgets import Markdown, Static, TextArea

from commandertui import preview, preview_docs, preview_screen
from commandertui.preview import PreviewKind, PreviewResult
from commandertui.preview_screen import PreviewScreen
from commandertui.widgets import FnButton


async def _skip_boot(pilot) -> None:
    await pilot.pause()
    for _ in range(4):
        await pilot.press("enter")
        await pilot.pause()
        if pilot.app.screen.__class__.__name__ != "BootScreen":
            break


async def _open_preview(pilot, app, name: str) -> PreviewScreen:
    app._panel("left").move_cursor_to(name)
    await pilot.press("v")
    screen = app.screen
    assert isinstance(screen, PreviewScreen)
    await _wait_loaded(pilot, screen)
    return screen


async def _wait_loaded(pilot, screen: PreviewScreen) -> None:
    await pilot.pause(0.05)
    deadline = time.monotonic() + 20
    while (screen._loading or screen.result is None) and time.monotonic() < deadline:
        await pilot.pause(0.05)
    await pilot.pause(0.1)


def _visible_actions(screen: PreviewScreen) -> list[str]:
    return [btn.action_name for btn in screen.query(FnButton) if btn.display]


def _app(tmp_path):
    from commandertui.app import CommanderApp

    other = tmp_path.parent / "other_pane"
    other.mkdir(exist_ok=True)
    return CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)


# ------------------------------------------------------------------ text decoding and limits


@pytest.mark.parametrize("offset", range(4))
def test_byte_truncation_inside_a_multibyte_character_stays_utf8(tmp_path, offset):
    path = tmp_path / "big.txt"
    path.write_bytes(("x" * offset + "ñ" * 200).encode("utf-8"))
    res = preview.load_preview(str(path), text_byte_limit=101)
    assert res.encoding == "utf-8"
    assert res.truncated_reason is not None
    assert "Ã" not in res.text and set(res.text) <= {"x", "ñ"}


def test_invalid_bytes_still_fall_back_to_cp1252_when_truncated(tmp_path):
    path = tmp_path / "latin.txt"
    path.write_bytes("año ".encode("latin-1") * 100)
    res = preview.load_preview(str(path), text_byte_limit=50)
    assert res.encoding == "cp1252 (lossy)"
    assert "año" in res.text


def test_file_with_exactly_the_line_limit_is_not_reported_truncated(tmp_path):
    exact = tmp_path / "exact.txt"
    exact.write_bytes(b"a\n" * 5)
    res = preview.load_preview(str(exact), text_line_limit=5)
    assert res.truncated_reason is None
    assert res.text.count("\n") == 5

    longer = tmp_path / "longer.txt"
    longer.write_bytes(b"a\n" * 6)
    res = preview.load_preview(str(longer), text_line_limit=5)
    assert res.truncated_reason == "Truncated at 5 lines"
    assert res.text.count("\n") == 5


def test_dotfiles_listed_as_text_are_classified_as_text():
    for name in (".env", ".gitignore", ".editorconfig"):
        assert preview._classify_by_extension(name)[0] == "text"
    assert preview._classify_by_extension(".unknownrc")[0] == "unknown"


def test_fallback_metadata_never_reports_a_missing_mime_type():
    assert preview._minimal_fallback_metadata("/x/report.pdf").mime_type == "application/pdf"
    assert preview._minimal_fallback_metadata("/x/noext").mime_type == "application/octet-stream"


def test_isolated_worker_starts_from_another_working_directory(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_text("hola\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    res = preview.load_preview_isolated(str(target))
    assert res.kind == PreviewKind.TEXT, res.error
    assert res.text == "hola\n"


# ------------------------------------------------------------------ install hints


def test_optional_engine_hint_matches_the_install_kind(monkeypatch):
    monkeypatch.setattr(preview, "_install_kind", "pip")
    assert preview.optional_engine_hint("images") == 'install with: pip install "commandertui[images]"'
    monkeypatch.setattr(preview, "_install_kind", "pacman")
    assert "yay -S python-textual-image" in preview.optional_engine_hint("images")
    assert "pacman -S poppler" in preview.optional_engine_hint("pdf-render")
    assert "pip install" not in preview.optional_engine_hint("pdf-render")


def test_install_kind_is_handed_to_the_worker_through_the_environment(monkeypatch):
    monkeypatch.setattr(preview, "_install_kind", None)
    monkeypatch.setenv("COMMANDERTUI_INSTALL_KIND", "pacman")
    assert preview.install_kind() == "pacman"


# ------------------------------------------------------------------ CSV delimiter


def test_csv_with_semicolons_is_split_into_columns(tmp_path):
    path = tmp_path / "semi.csv"
    path.write_text("nombre;edad;ciudad\nAna;30;Córdoba\nLuis;41;Salta\n", encoding="utf-8")
    res = preview.load_preview(str(path))
    assert res.kind == PreviewKind.TABLE
    assert res.table_headers == ["nombre", "edad", "ciudad"]
    assert res.table_rows == [["Ana", "30", "Córdoba"], ["Luis", "41", "Salta"]]


def test_csv_delimiter_detection_prefers_comma_and_keeps_single_column():
    assert preview_docs._detect_csv_delimiter("a,b,c\n1,2,3\n") == ","
    assert preview_docs._detect_csv_delimiter("a,b;c\n1,2;3\n") == ","
    assert preview_docs._detect_csv_delimiter("solo\nuna\ncolumna\n") == ","
    assert preview_docs._detect_csv_delimiter('"x;y",b\n"1;2",3\n') == ","
    assert preview_docs._detect_csv_delimiter("a|b|c\n1|2|3\n") == "|"


# ------------------------------------------------------------------ workbook sheets


def _workbook_with_chart_sheet(path) -> None:
    import openpyxl
    from openpyxl.chart import BarChart, Reference

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append([1, 2, 3])
    chart_sheet = wb.create_chartsheet("Chart")
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=1, min_row=1, max_col=3, max_row=1))
    chart_sheet.add_chart(chart)
    wb.create_sheet("Third").append(["z"])
    wb.save(path)


needs_openpyxl = pytest.mark.skipif(
    not preview_docs.is_xlsx_engine_available(), reason="optional [documents] dependencies not installed"
)


@needs_openpyxl
def test_chart_sheet_keeps_the_sheet_list(tmp_path):
    path = tmp_path / "chart.xlsx"
    _workbook_with_chart_sheet(path)
    res = preview.load_preview(str(path), sheet_index=1)
    assert res.kind == PreviewKind.TABLE
    assert res.sheet_names == ["Data", "Chart", "Third"]
    assert res.sheet_index == 1
    assert res.table_rows == []
    assert "chart sheet" in res.note


@needs_openpyxl
async def test_viewer_walks_past_a_chart_sheet(tmp_path):
    _workbook_with_chart_sheet(tmp_path / "chart.xlsx")
    app = _app(tmp_path)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        screen = await _open_preview(pilot, app, "chart.xlsx")
        for expected in (1, 2):
            await pilot.press("right_square_bracket")
            await _wait_loaded(pilot, screen)
            assert screen.result.kind == PreviewKind.TABLE
            assert screen.result.sheet_index == expected
        assert screen.result.table_rows == [["z"]]
        assert "prev_item" in _visible_actions(screen)


@needs_openpyxl
async def test_failed_sheet_switch_keeps_the_current_sheet_and_navigation(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["first"])
    wb.create_sheet("Second").append(["second"])
    wb.save(tmp_path / "two.xlsx")

    app = _app(tmp_path)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        screen = await _open_preview(pilot, app, "two.xlsx")
        assert screen.result.table_rows == [["first"]]

        def broken(path, **kwargs):
            return PreviewResult(
                kind=PreviewKind.ERROR,
                path=path,
                metadata=preview._minimal_fallback_metadata(path),
                error="Preview worker timed out after 12.0s.",
            )

        with patch.object(preview, "load_preview_isolated", side_effect=broken):
            await pilot.press("right_square_bracket")
            await pilot.pause(0.3)

        assert screen.result.kind == PreviewKind.TABLE
        assert screen.result.table_rows == [["first"]]
        assert screen.sheet_index == 0
        notice = screen.query_one("#preview-notice", Static)
        assert notice.display
        assert "Could not load sheet 2" in str(notice.content)
        assert "next_item" in _visible_actions(screen)

        await pilot.press("right_square_bracket")
        await _wait_loaded(pilot, screen)
        assert screen.result.table_rows == [["second"]]


# ------------------------------------------------------------------ Markdown size guard


def test_markdown_block_estimate_counts_paragraphs_items_and_table_rows():
    text = "# Title\n\nSome text.\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    assert preview_screen.estimate_markdown_blocks(text) == 4 + 2 + 3
    assert preview_screen.estimate_markdown_blocks("") == 0


async def test_large_markdown_is_shown_as_source_without_rendering(tmp_path):
    block = "Some *text* here.\n\n- item one\n- item two\n\n"
    (tmp_path / "big.md").write_text("# Title\n\n" + block * 400, encoding="utf-8")
    app = _app(tmp_path)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        started = time.monotonic()
        screen = await _open_preview(pilot, app, "big.md")
        assert time.monotonic() - started < 8
        assert screen.markdown_too_large
        assert not screen.query(Markdown)
        assert "# Title" in screen.query_one("#preview-text", TextArea).text
        assert "toggle_markdown_source" not in _visible_actions(screen)
        assert "toggle_wrap" in _visible_actions(screen)
        assert "Large Markdown" in str(screen.query_one("#preview-notice", Static).content)

        await pilot.press("m")
        await pilot.pause()
        assert not screen.query(Markdown)


# ------------------------------------------------------------------ PDF visual button


async def test_pdf_visual_button_is_hidden_without_a_renderer(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    app = _app(tmp_path)

    def text_only(path, **kwargs):
        return PreviewResult(
            kind=PreviewKind.PDF,
            path=path,
            metadata=preview._minimal_fallback_metadata(path),
            text="hello",
            page_count=2,
            pdf_visual_available=False,
        )

    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        with patch.object(preview, "load_preview_isolated", side_effect=text_only):
            screen = await _open_preview(pilot, app, "doc.pdf")
        actions = _visible_actions(screen)
        assert "toggle_pdf_visual" not in actions
        assert "next_item" in actions


# ------------------------------------------------------------------ PDF pages through poppler


def _blank_pdf(path, pages: int = 2) -> None:
    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=300)
    with open(path, "wb") as fh:
        writer.write(fh)


needs_poppler = pytest.mark.skipif(
    preview_docs.poppler_pdftoppm() is None
    or not preview.is_image_engine_available()
    or not preview_docs.is_pdf_text_engine_available(),
    reason="poppler (pdftoppm), pypdf and the image engine are needed",
)


@needs_poppler
def test_pdf_page_renders_with_poppler_when_pdfium_is_missing(tmp_path):
    path = tmp_path / "scan.pdf"
    _blank_pdf(path)
    with patch.object(preview_docs, "is_pdfium_available", return_value=False):
        assert preview_docs.is_pdf_render_engine_available()
        res = preview.load_preview(str(path), pdf_visual=True, page_index=1)
    assert res.kind == PreviewKind.PDF and res.pdf_visual_mode
    assert res.page_index == 1 and res.page_count == 2
    assert res.pil_image is not None
    assert max(res.pil_image.size) == preview.MAX_IMAGE_DIM
    assert res.pil_image.width < res.pil_image.height


@needs_poppler
def test_poppler_takes_over_when_pdfium_is_installed_but_broken(tmp_path):
    path = tmp_path / "scan.pdf"
    _blank_pdf(path, pages=1)
    with (
        patch.object(preview_docs, "is_pdfium_available", return_value=True),
        patch.object(preview_docs, "_render_pdf_page_image", side_effect=OSError("libpdfium.so missing")),
    ):
        res = preview.load_preview(str(path), pdf_visual=True)
    assert res.pdf_visual_mode and res.pil_image is not None


def test_no_page_renderer_without_pdfium_and_poppler(monkeypatch):
    monkeypatch.setenv("COMMANDERTUI_DISABLE_POPPLER", "1")
    with patch.object(preview_docs, "is_pdfium_available", return_value=False):
        assert preview_docs.poppler_pdftoppm() is None
        assert not preview_docs.is_pdf_render_engine_available()


@needs_poppler
def test_poppler_failure_is_reported_not_raised(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nnot really a pdf\n")
    with patch.object(preview_docs, "_pdf_page_count", return_value=1):
        img, _, _, _, err = preview_docs._render_pdf_page_poppler(
            path.read_bytes(), str(path), 0, pixel_limit=10**7, max_dim=400, timeout=10
        )
    assert img is None and err.startswith("Could not render PDF page")


# ------------------------------------------------------------------ open with the system viewer


def test_system_opener_needs_linux_a_display_and_xdg_open(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(preview.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert preview.system_opener() is None
    assert "cannot open" in preview.open_with_system_app("/etc/hostname")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    assert preview.system_opener() == "/usr/bin/xdg-open"
    monkeypatch.setattr(preview.shutil, "which", lambda name: None)
    assert preview.system_opener() is None
    monkeypatch.setattr("sys.platform", "darwin")
    assert preview.system_opener() is None


def test_open_with_system_app_starts_xdg_open_detached(tmp_path, monkeypatch):
    target = tmp_path / "doc.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(preview, "system_opener", lambda: "/usr/bin/xdg-open")
    with patch.object(preview.subprocess, "Popen") as popen:
        assert preview.open_with_system_app(str(target)) is None
    args, kwargs = popen.call_args
    assert args[0] == ["/usr/bin/xdg-open", str(target)]
    assert kwargs["start_new_session"] is True
    assert preview.open_with_system_app(str(tmp_path / "missing.pdf")) == "File not found."


async def test_viewer_o_key_opens_the_system_viewer_on_linux(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hola\n", encoding="utf-8")
    monkeypatch.setattr(preview, "is_macos", lambda: False)
    monkeypatch.setattr(preview, "system_opener", lambda: "/usr/bin/xdg-open")
    app = _app(tmp_path)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        screen = await _open_preview(pilot, app, "a.txt")
        assert "quick_look" in _visible_actions(screen)
        with patch.object(preview, "open_with_system_app", return_value=None) as opened:
            await pilot.press("o")
            await pilot.pause()
        opened.assert_called_once_with(screen.path)
        assert "Opened" in str(screen.query_one("#preview-notice", Static).content)


async def test_viewer_hides_open_button_without_a_desktop(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hola\n", encoding="utf-8")
    monkeypatch.setattr(preview, "is_macos", lambda: False)
    monkeypatch.setattr(preview, "system_opener", lambda: None)
    app = _app(tmp_path)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        screen = await _open_preview(pilot, app, "a.txt")
        assert "quick_look" not in _visible_actions(screen)
