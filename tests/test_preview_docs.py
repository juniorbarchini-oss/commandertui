import io
import os
import threading
import zipfile
from unittest.mock import patch

import pytest
from textual.widgets import DataTable, Static, TextArea

from commandertui import preview, preview_docs
from commandertui.modals import ChoiceScreen
from commandertui.preview import PreviewKind
from commandertui.preview_screen import PreviewScreen


@pytest.fixture(autouse=True)
def _pdfium_only(monkeypatch):
    """These tests cover the PDFium renderer; the poppler fallback (which would rescue the
    simulated PDFium failures on machines that have pdftoppm) is tested in test_preview_fixes.py."""
    monkeypatch.setenv("COMMANDERTUI_DISABLE_POPPLER", "1")


async def _skip_boot(pilot) -> None:
    await pilot.pause()
    for _ in range(4):
        await pilot.press("enter")
        await pilot.pause()
        if pilot.app.screen.__class__.__name__ != "BootScreen":
            break


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _make_text_pdf(pages_text: list[str]) -> bytes:
    """Build a valid minimal multi-page PDF with Type1 Helvetica text streams."""
    objects: list[bytes] = []
    # 1: Catalog, 2: Pages, 3: Font, then per-page: Page obj + Content obj
    n = len(pages_text)
    page_ids = [4 + 2 * i for i in range(n)]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)

    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(f"2 0 obj\n<< /Type /Pages /Kids [ {kids} ] /Count {n} >>\nendobj\n".encode("ascii"))
    objects.append(b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")

    for i, text in enumerate(pages_text):
        p_id = 4 + 2 * i
        c_id = p_id + 1
        stream = f"BT /F1 12 Tf 50 720 Td ({_pdf_escape(text)}) Tj ET\n".encode("latin-1", errors="replace")
        page_obj = (
            f"{p_id} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {c_id} 0 R >>\nendobj\n"
        ).encode("ascii")
        content_obj = (
            f"{c_id} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"endstream\nendobj\n"
        )
        objects.append(page_obj)
        objects.append(content_obj)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)

    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n").encode("ascii")
    )
    return bytes(out)


def _minimal_ooxml_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CSV / TSV (works in base install without optional extras)
# ---------------------------------------------------------------------------


def test_load_csv_and_tsv_tables_and_bounds(tmp_path):
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "\ufeffName,Note,Extra\nAlice,\x1b[31mred\x1b[0m\nBob,[red]literal[/red],3,overflow\nCaro,4,5\nDave,6,7\n",
        encoding="utf-8",
    )
    res = preview.load_preview(str(csv_path))
    assert res.kind == PreviewKind.TABLE
    assert res.table_headers == ["Name", "Note", "Extra", "Col 4"]
    assert res.table_rows is not None
    assert "\x1b" not in res.table_rows[0][1]
    assert "[red]literal[/red]" == res.table_rows[1][1]

    # Row, column, and cell caps
    meta, st, _, _ = preview.collect_metadata(str(csv_path))
    capped = preview_docs.load_csv_preview(
        str(csv_path),
        meta,
        st,
        None,
        max_rows=3,
        max_cols=2,
        max_cells=3,
    )
    assert capped.kind == PreviewKind.TABLE
    assert len(capped.table_headers or []) == 2
    assert capped.truncated_reason is not None

    tsv_path = tmp_path / "items.tsv"
    tsv_path.write_text("Key\tValue\nk1\tv1\n", encoding="utf-8")
    res_tsv = preview.load_preview(str(tsv_path))
    assert res_tsv.kind == PreviewKind.TABLE
    assert res_tsv.table_headers == ["Key", "Value"]
    assert res_tsv.table_rows == [["k1", "v1"]]


@pytest.mark.asyncio
async def test_preview_screen_csv_renders_datatable_and_markup_safely(tmp_path):
    csv_path = tmp_path / "table.csv"
    csv_path.write_text("ColA,ColB\nhello,[[badmarkup]\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        app._panel("left").move_cursor_to("table.csv")
        await pilot.press("v")
        await pilot.pause(0.35)
        assert isinstance(app.screen, PreviewScreen)
        dt = app.screen.query_one("#preview-table", DataTable)
        assert dt.row_count == 1
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


# ---------------------------------------------------------------------------
# Legacy Office / OpenDocument & base-without-extras fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected_fragment",
    [
        ("legacy.xls", "Legacy Excel"),
        ("legacy.doc", "Legacy Word"),
        ("calc.ods", "OpenDocument"),
        ("macro.xlsm", "Macro-enabled"),
    ],
)
def test_legacy_and_opendocument_return_metadata_without_external_tools(
    tmp_path, filename, expected_fragment
):
    target = tmp_path / filename
    target.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1dummy")
    with patch("subprocess.Popen") as mock_popen, patch("subprocess.run") as mock_run:
        res = preview.load_preview(str(target))
    assert res.kind == PreviewKind.METADATA
    assert expected_fragment in res.metadata.file_type
    assert "no external converters" in (res.note or "")
    mock_popen.assert_not_called()
    mock_run.assert_not_called()


def test_documents_without_optional_extras_fallback_to_metadata(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(_make_text_pdf(["Hello"]))
    docx_file = tmp_path / "b.docx"
    docx_file.write_bytes(b"PK\x03\x04dummy")
    xlsx_file = tmp_path / "c.xlsx"
    xlsx_file.write_bytes(b"PK\x03\x04dummy")

    with (
        patch.object(preview_docs, "is_pdf_text_engine_available", return_value=False),
        patch.object(preview_docs, "is_pdf_render_engine_available", return_value=False),
        patch.object(preview_docs, "is_docx_engine_available", return_value=False),
        patch.object(preview_docs, "is_xlsx_engine_available", return_value=False),
    ):
        for target in (pdf, docx_file, xlsx_file):
            res = preview.load_preview(str(target))
            assert res.kind == PreviewKind.METADATA
            assert "commandertui[documents]" in (res.note or "")


# ---------------------------------------------------------------------------
# PDF preview, pagination, security, limits, and UI
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not preview_docs.is_pdf_text_engine_available(),
    reason="optional [documents] dependencies not installed",
)
def test_pdf_loader_multipage_limits_encrypted_and_corrupt(tmp_path):
    pdf_path = tmp_path / "multi.pdf"
    pdf_path.write_bytes(_make_text_pdf(["Page One \x1b[31malert", "Page Two data", "Page Three end"]))

    res0 = preview.load_preview(str(pdf_path), page_index=0)
    assert res0.kind == PreviewKind.PDF
    assert res0.page_count == 3
    assert res0.page_index == 0
    assert "Page One" in (res0.text or "")
    assert "\x1b" not in (res0.text or "")

    res1 = preview.load_preview(str(pdf_path), page_index=1)
    assert res1.page_index == 1
    assert "Page Two data" in (res1.text or "")

    # Clamp out-of-range page index
    res_end = preview.load_preview(str(pdf_path), page_index=99)
    assert res_end.page_index == 2
    assert "Page Three end" in (res_end.text or "")

    # Char limit truncation
    meta, st, _, _ = preview.collect_metadata(str(pdf_path))
    res_short = preview_docs.load_pdf_preview(str(pdf_path), meta, st, None, max_chars=8)
    assert len(res_short.text or "") == 8
    assert res_short.truncated_reason is not None

    # Scanned/blank page note
    blank_pdf = tmp_path / "blank.pdf"
    blank_pdf.write_bytes(_make_text_pdf([""]))
    res_blank = preview.load_preview(str(blank_pdf))
    assert res_blank.kind == PreviewKind.PDF
    assert "No selectable text" in (res_blank.note or "")

    # Encrypted PDF
    import pypdf

    enc_path = tmp_path / "secret.pdf"
    writer = pypdf.PdfWriter()
    writer.append(io.BytesIO(pdf_path.read_bytes()))
    writer.encrypt("topsecret")
    with open(enc_path, "wb") as fh:
        writer.write(fh)
    res_enc = preview.load_preview(str(enc_path))
    assert res_enc.kind == PreviewKind.METADATA
    assert "Encrypted" in (res_enc.note or "")

    # Corrupt PDF
    bad_path = tmp_path / "corrupt.pdf"
    bad_path.write_bytes(b"%PDF-1.7\nnot-a-valid-xref")
    res_bad = preview.load_preview(str(bad_path))
    assert res_bad.kind == PreviewKind.METADATA
    assert "Could not parse PDF" in (res_bad.note or "")


@pytest.mark.skipif(
    not preview_docs.is_pdf_render_engine_available(),
    reason="optional [pdf-render] dependencies not installed",
)
def test_pdf_visual_render_in_isolated_worker(tmp_path):
    pdf_path = tmp_path / "visual.pdf"
    pdf_path.write_bytes(_make_text_pdf(["First visual", "Second visual"]))

    res = preview.load_preview_isolated(str(pdf_path), page_index=1, pdf_visual=True, image_max_dim=400)
    assert res.kind == PreviewKind.PDF
    assert res.pdf_visual_mode is True
    assert res.page_index == 1
    assert res.page_count == 2
    assert res.pil_image is not None
    assert max(res.pil_image.size) <= 400


@pytest.mark.skipif(
    not preview_docs.is_pdf_text_engine_available(),
    reason="optional [documents] dependencies not installed",
)
@pytest.mark.asyncio
async def test_preview_screen_pdf_pagination_visual_toggle_and_cancellation(tmp_path):
    pdf_path = tmp_path / "manual.pdf"
    pdf_path.write_bytes(_make_text_pdf(["Alpha page", "Beta page", "Gamma page"]))
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        app._panel("left").move_cursor_to("manual.pdf")
        await pilot.press("v")
        await pilot.pause(0.4)
        assert isinstance(app.screen, PreviewScreen)
        ta = app.screen.query_one("#preview-text", TextArea)
        assert "Alpha page" in ta.text
        assert "Page 1/3" in str(app.screen.query_one("#preview-header", Static).content)

        await pilot.press("]")
        await pilot.pause(0.4)
        ta2 = app.screen.query_one("#preview-text", TextArea)
        assert "Beta page" in ta2.text
        assert "Page 2/3" in str(app.screen.query_one("#preview-header", Static).content)

        await pilot.press("[")
        await pilot.pause(0.4)
        assert "Alpha page" in app.screen.query_one("#preview-text", TextArea).text

        if preview_docs.is_pdf_render_engine_available():
            await pilot.press("g")
            await pilot.pause(0.45)
            assert app.screen.query_one("#preview-image") is not None
            await pilot.press("f")
            await pilot.pause()
            assert app.screen.image_mode_index == 1
            await pilot.press("g")
            await pilot.pause(0.4)
            assert "Alpha page" in app.screen.query_one("#preview-text", TextArea).text

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.skipif(
    not preview_docs.is_pdf_text_engine_available(),
    reason="optional [documents] dependencies not installed",
)
@pytest.mark.asyncio
async def test_preview_screen_pdf_rapid_page_change_ignores_stale_worker(tmp_path):
    pdf_path = tmp_path / "pages.pdf"
    pdf_path.write_bytes(_make_text_pdf(["First page", "Second page", "Third page"]))
    other = tmp_path / "other"
    other.mkdir()

    gate_page1 = threading.Event()

    def gated_loader(path, cancel=None, **kwargs):
        page_idx = kwargs.get("page_index", 0)
        if page_idx == 1:
            gate_page1.wait(timeout=1.5)
        return preview.load_preview(path, cancel=cancel, **kwargs)

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    with patch.object(preview, "load_preview_isolated", side_effect=gated_loader):
        async with app.run_test(size=(140, 30)) as pilot:
            await _skip_boot(pilot)
            app._panel("left").move_cursor_to("pages.pdf")
            await pilot.press("v")
            await pilot.pause(0.15)
            await pilot.press("]")  # requests page 1 (blocked)
            await pilot.press("]")  # supersedes with page 2 (fast)
            await pilot.pause(0.25)
            assert "Third page" in app.screen.query_one("#preview-text", TextArea).text
            gate_page1.set()
            await pilot.pause(0.15)
            # Stale page 1 did not overwrite page 2
            assert "Third page" in app.screen.query_one("#preview-text", TextArea).text


# ---------------------------------------------------------------------------
# DOCX preview, tables order, security bounds
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not preview_docs.is_docx_engine_available(),
    reason="optional [documents] dependencies not installed",
)
def test_docx_loader_order_sanitization_and_safety_checks(tmp_path):
    import docx

    doc_path = tmp_path / "sample.docx"
    d = docx.Document()
    d.add_heading("Executive Summary", level=1)
    d.add_paragraph("Opening \x9b31mline.")
    tbl = d.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "Metric"
    tbl.cell(0, 1).text = "Score"
    tbl.cell(1, 0).text = "Speed"
    tbl.cell(1, 1).text = "Fast"
    d.add_paragraph("Closing note.")
    d.save(doc_path)

    res = preview.load_preview(str(doc_path))
    assert res.kind == PreviewKind.TEXT
    text = res.text or ""
    assert "\x9b" not in text
    assert (
        text.index("Executive Summary")
        < text.index("Opening")
        < text.index("Metric")
        < text.index("Closing note.")
    )

    # Zip bomb (actual uncompressed size exceeds cap even with small cap)
    bomb_path = tmp_path / "bomb.docx"
    bomb_path.write_bytes(
        _minimal_ooxml_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"A" * 4096})
    )
    meta, st, _, _ = preview.collect_metadata(str(bomb_path))
    res_bomb = preview_docs.load_docx_preview(str(bomb_path), meta, st, None, max_uncompressed=1024)
    assert res_bomb.kind == PreviewKind.METADATA
    assert "decompressed size limit" in (res_bomb.note or "").lower()

    # Dangerous XML DTD/ENTITY
    xxe_path = tmp_path / "xxe.docx"
    xxe_path.write_bytes(
        _minimal_ooxml_zip(
            {
                "[Content_Types].xml": b"<Types/>",
                "word/document.xml": b'<!DOCTYPE foo [<!ENTITY x "1">]><doc/>',
            }
        )
    )
    res_xxe = preview.load_preview(str(xxe_path))
    assert res_xxe.kind == PreviewKind.METADATA
    assert "xml dtd/entity" in (res_xxe.note or "").lower()

    # Corrupt & OLE2 encrypted DOCX
    bad_docx = tmp_path / "bad.docx"
    bad_docx.write_bytes(b"not-a-zip")
    assert preview.load_preview(str(bad_docx)).kind == PreviewKind.METADATA

    enc_docx = tmp_path / "enc.docx"
    enc_docx.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1encrypted-package")
    res_enc = preview.load_preview(str(enc_docx))
    assert res_enc.kind == PreviewKind.METADATA
    assert "encrypted" in (res_enc.note or "").lower()


# ---------------------------------------------------------------------------
# XLSX preview, sheets navigation, formulas as data, limits, and UI
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not preview_docs.is_xlsx_engine_available(),
    reason="optional [documents] dependencies not installed",
)
def test_xlsx_loader_sheets_formulas_and_limits(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Overview"
    ws1["A1"] = "Item"
    ws1["B1"] = "Formula"
    ws1["A2"] = 10
    ws1["B2"] = "=A2*2"
    ws1["A3"] = "\x9b32mtext"
    ws2 = wb.create_sheet("Summary")
    ws2["A1"] = "S2"
    ws3 = wb.create_sheet("Third")
    ws3["A1"] = "S3"
    xlsx_path = tmp_path / "book.xlsx"
    wb.save(xlsx_path)

    res1 = preview.load_preview(str(xlsx_path), sheet_index=0)
    assert res1.kind == PreviewKind.TABLE
    assert res1.sheet_names == ["Overview", "Summary", "Third"]
    assert res1.sheet_index == 0
    assert res1.table_rows is not None
    assert res1.table_rows[1] == ["10", "=A2*2"]
    assert "\x9b" not in res1.table_rows[2][0]

    res2 = preview.load_preview(str(xlsx_path), sheet_index=1)
    assert res2.sheet_index == 1
    assert res2.table_rows == [["S2"]]

    meta, st, _, _ = preview.collect_metadata(str(xlsx_path))
    capped = preview_docs.load_xlsx_preview(str(xlsx_path), meta, st, None, max_rows=2, max_cols=1)
    assert capped.kind == PreviewKind.TABLE
    assert len(capped.table_rows or []) == 2
    assert len(capped.table_headers or []) == 1
    assert capped.truncated_reason is not None

    # Corrupt XLSX
    corrupt = tmp_path / "corrupt.xlsx"
    corrupt.write_bytes(b"PK\x03\x04broken")
    assert preview.load_preview(str(corrupt)).kind == PreviewKind.METADATA


@pytest.mark.skipif(
    not preview_docs.is_xlsx_engine_available(),
    reason="optional [documents] dependencies not installed",
)
@pytest.mark.asyncio
async def test_preview_screen_xlsx_sheet_navigation_and_picker(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "SheetOne"
    ws1["A1"] = "FirstSheetCell"
    ws2 = wb.create_sheet("SheetTwo")
    ws2["A1"] = "SecondSheetCell"
    ws3 = wb.create_sheet("SheetThree")
    ws3["A1"] = "ThirdSheetCell"
    xlsx_path = tmp_path / "sheets.xlsx"
    wb.save(xlsx_path)
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        app._panel("left").move_cursor_to("sheets.xlsx")
        await pilot.press("v")
        await pilot.pause(0.45)
        assert isinstance(app.screen, PreviewScreen)
        assert "SheetOne" in str(app.screen.query_one("#preview-header", Static).content)

        await pilot.press("]")
        await pilot.pause(0.45)
        assert "SheetTwo" in str(app.screen.query_one("#preview-header", Static).content)

        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.press("down", "enter")
        await pilot.pause(0.45)
        assert isinstance(app.screen, PreviewScreen)
        assert "SheetThree" in str(app.screen.query_one("#preview-header", Static).content)

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


def test_csv_rectangular_cell_cap_blocks_padding_overflow(tmp_path):
    csv_path = tmp_path / "ragged.csv"
    lines = ["h1"] + [f"r{i}" for i in range(1, 10)] + [",".join([f"c{j}" for j in range(10)])]
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    meta, st, _, _ = preview.collect_metadata(str(csv_path))
    res = preview_docs.load_csv_preview(str(csv_path), meta, st, None, max_cells=20)
    assert res.kind == PreviewKind.TABLE
    rows = res.table_rows or []
    cols = res.table_headers or []
    assert (1 + len(rows)) * len(cols) <= 20
    assert "Cell cap" in (res.truncated_reason or "")


@pytest.mark.skipif(
    not preview_docs.is_pdf_render_engine_available(),
    reason="optional [pdf-render] dependencies not installed",
)
def test_pdf_render_huge_page_respects_max_dim_without_giant_allocation(tmp_path):
    import pypdfium2

    doc = pypdfium2.PdfDocument.new()
    doc.new_page(1_000_000, 1_000_000)
    huge_pdf = tmp_path / "huge_page.pdf"
    with open(huge_pdf, "wb") as fh:
        doc.save(fh)
    doc.close()

    res = preview_docs.load_pdf_preview(
        str(huge_pdf),
        *preview.collect_metadata(str(huge_pdf))[:2],
        None,
        render_visual=True,
        max_dim=800,
        pixel_limit=1_000_000,
    )
    assert res.kind == PreviewKind.PDF
    assert res.pdf_visual_mode is True
    assert res.pil_image is not None
    assert max(res.pil_image.size) <= 800
    assert res.pil_image.width * res.pil_image.height <= 1_000_000

    # Impossible aspect ratio / non-finite size is rejected BEFORE page.render()
    raw_normal = _make_text_pdf(["Hello"])
    fake_page = type(
        "_FakePage",
        (),
        {
            "get_size": staticmethod(lambda: (1e12, 1.0)),
            "render": staticmethod(lambda **_kw: (_ for _ in ()).throw(AssertionError("render called"))),
            "close": staticmethod(lambda: None),
        },
    )()

    class _FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, _idx):
            return fake_page

        def close(self):
            pass

    with patch.object(pypdfium2, "PdfDocument", return_value=_FakeDoc()):
        img, clamped, cnt, dims, err = preview_docs._render_pdf_page_image(
            raw_normal, 0, pixel_limit=25_000_000, max_dim=1600
        )
    assert img is None
    assert cnt == 1
    assert err and "scale bounds" in err.lower()


@pytest.mark.skipif(
    not preview_docs.is_pdf_render_engine_available(),
    reason="optional [pdf-render] dependencies not installed",
)
def test_pdf_render_only_without_pypdf_and_fallback_preserves_pagination(tmp_path):
    pdf_path = tmp_path / "render_only.pdf"
    pdf_path.write_bytes(_make_text_pdf(["Page One Text", "Page Two Text", "Page Three Text"]))
    meta, st, _, _ = preview.collect_metadata(str(pdf_path))

    # Even if pypdf is absent, [pdf-render] alone supports text, visual toggle, and pagination
    with patch.object(preview_docs, "is_pdf_text_engine_available", return_value=False):
        res_text = preview_docs.load_pdf_preview(str(pdf_path), meta, st, None, page_index=1)
        assert res_text.kind == PreviewKind.PDF
        assert res_text.page_count == 3
        assert res_text.page_index == 1
        assert res_text.pdf_visual_available is True
        assert "Page Two Text" in (res_text.text or "")

        res_vis = preview_docs.load_pdf_preview(
            str(pdf_path), meta, st, None, page_index=2, render_visual=True, max_dim=300
        )
        assert res_vis.kind == PreviewKind.PDF
        assert res_vis.pdf_visual_mode is True
        assert res_vis.page_index == 2
        assert res_vis.pil_image is not None

    # Failed visual render returns PreviewKind.PDF fallback (preserving g & pagination), not METADATA
    with patch.object(
        preview_docs,
        "_render_pdf_page_image",
        return_value=(None, 1, 3, None, "Simulated render failure."),
    ):
        res_fallback = preview_docs.load_pdf_preview(
            str(pdf_path), meta, st, None, page_index=1, render_visual=True
        )
    assert res_fallback.kind == PreviewKind.PDF
    assert res_fallback.pdf_visual_mode is False
    assert res_fallback.pdf_visual_available is True
    assert res_fallback.page_index == 1
    assert res_fallback.page_count == 3
    assert "Page Two Text" in (res_fallback.text or "")
    assert "Simulated render failure" in (res_fallback.note or "")


@pytest.mark.skipif(
    not preview_docs.is_xlsx_engine_available(),
    reason="optional [documents] dependencies not installed",
)
def test_xlsx_rectangular_cell_cap_and_bounded_iter_rows_with_far_column(tmp_path):
    import openpyxl

    # Sparse sheet with A1:A10 and J10 (rectangular 10x10 = 100 cells, must trigger max_cells=20)
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in range(1, 11):
        ws[f"A{r}"] = r
    ws["J10"] = "=A1*9"
    sparse_path = tmp_path / "sparse.xlsx"
    wb.save(sparse_path)

    meta, st, _, _ = preview.collect_metadata(str(sparse_path))
    res = preview_docs.load_xlsx_preview(str(sparse_path), meta, st, None, max_cells=20)
    assert res.kind == PreviewKind.TABLE
    assert len(res.table_rows or []) * len(res.table_headers or []) <= 20
    assert "Cell cap" in (res.truncated_reason or "")

    # Far column XFD1: must detect column truncation while keeping iter_rows bounded to max_cols+1
    wb_far = openpyxl.Workbook()
    ws_far = wb_far.active
    ws_far["A1"] = "first"
    ws_far["XFD1"] = "far_right"
    far_path = tmp_path / "far.xlsx"
    wb_far.save(far_path)

    meta_far, st_far, _, _ = preview.collect_metadata(str(far_path))
    res_far = preview_docs.load_xlsx_preview(
        str(far_path), meta_far, st_far, None, max_rows=5, max_cols=10, max_cells=100
    )
    assert res_far.kind == PreviewKind.TABLE
    assert len(res_far.table_headers or []) <= 10
    assert "Columns capped" in (res_far.truncated_reason or "")


@pytest.mark.skipif(
    not preview_docs.is_pdf_render_engine_available(),
    reason="optional [pdf-render] dependencies not installed",
)
@pytest.mark.asyncio
async def test_preview_screen_pdf_visual_fallback_keeps_g_and_pagination(tmp_path):
    pdf_path = tmp_path / "fallback_ui.pdf"
    pdf_path.write_bytes(_make_text_pdf(["First Fallback", "Second Fallback"]))
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    orig_render = preview_docs._render_pdf_page_image
    call_count = 0

    def fail_then_recover(raw, page_index, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None, page_index, 2, None, "Simulated visual failure."
        return orig_render(raw, page_index, **kwargs)

    with (
        patch.object(preview, "load_preview_isolated", side_effect=preview.load_preview),
        patch.object(preview_docs, "_render_pdf_page_image", side_effect=fail_then_recover),
    ):
        app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
        async with app.run_test(size=(140, 30)) as pilot:
            await _skip_boot(pilot)
            app._panel("left").move_cursor_to("fallback_ui.pdf")
            await pilot.press("v")
            await pilot.pause(0.3)
            assert isinstance(app.screen, PreviewScreen)
            assert "First Fallback" in app.screen.query_one("#preview-text", TextArea).text

            # Press g when visual render fails -> falls back to PreviewKind.PDF text, keeping g & ]
            await pilot.press("g")
            await pilot.pause(0.3)
            assert app.screen.pdf_visual is False
            assert app.screen.result is not None
            assert app.screen.result.pdf_visual_available is True
            assert "First Fallback" in app.screen.query_one("#preview-text", TextArea).text

            # Pagination still works right after fallback
            await pilot.press("]")
            await pilot.pause(0.3)
            assert "Second Fallback" in app.screen.query_one("#preview-text", TextArea).text

            # Pressing g again can still enter visual mode cleanly
            await pilot.press("g")
            await pilot.pause(0.4)
            assert app.screen.pdf_visual is True
            assert app.screen.query_one("#preview-image") is not None

            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.skipif(
    not preview_docs.is_pdf_render_engine_available(),
    reason="optional [pdf-render] dependencies not installed",
)
def test_pdf_scanned_image_only_and_unexpected_pypdf_error_do_not_crash(tmp_path):
    import pypdf
    import pypdfium2
    from PIL import Image as PILImage

    doc = pypdfium2.PdfDocument.new()
    img = PILImage.new("RGB", (800, 1000), color=(240, 240, 240))
    for _ in range(3):
        page = doc.new_page(595, 842)
        pdf_img = pypdfium2.PdfImage.new(doc)
        pdf_img.set_bitmap(pypdfium2.PdfBitmap.from_pil(img))
        pdf_img.set_matrix(pypdfium2.PdfMatrix().scale(595, 842))
        page.insert_obj(pdf_img)
        page.gen_content()
        page.close()
    scan_pdf = tmp_path / "scanned.pdf"
    with open(scan_pdf, "wb") as fh:
        doc.save(fh)
    doc.close()

    res_text = preview.load_preview_isolated(str(scan_pdf), pdf_visual=False)
    assert res_text.kind == PreviewKind.PDF
    assert res_text.page_count == 3
    assert "No selectable text" in (res_text.note or "")

    res_vis = preview.load_preview_isolated(str(scan_pdf), pdf_visual=True)
    assert res_vis.kind == PreviewKind.PDF
    assert res_vis.pdf_visual_mode is True
    assert res_vis.pil_image is not None

    meta, st, _, _ = preview.collect_metadata(str(scan_pdf))
    with patch.object(pypdf, "PdfReader", side_effect=TypeError("unexpected parser bug")):
        res_err = preview_docs.load_pdf_preview(str(scan_pdf), meta, st, None)
    assert res_err.kind == PreviewKind.METADATA
    assert res_err.pdf_visual_available is True
    assert "Could not parse PDF" in (res_err.note or "")


def test_isolated_worker_signal_kill_and_large_stderr_do_not_hang_or_crash(tmp_path):
    import json
    import subprocess
    import sys
    import time

    pdf_path = tmp_path / "noisy.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    orig_popen = subprocess.Popen

    # 1. Worker writing >128KB to stderr + exiting 0 must NOT deadlock on pipe buffer
    def noisy_success_popen(cmd, **kwargs):
        out_idx = cmd.index("--result-out") + 1
        result_out = cmd[out_idx]
        payload = {
            "kind": "metadata",
            "path": str(pdf_path),
            "metadata": {
                "name": "noisy.pdf",
                "path": str(pdf_path),
                "size_bytes": 15,
                "mtime": None,
                "ctime": None,
                "mode_str": "-rw-r--r--",
                "octal_mode": "644",
                "is_symlink": False,
                "symlink_target": None,
                "file_type": "PDF document",
                "mime_type": "application/pdf",
                "image_dimensions": None,
            },
            "note": "Recovered after noisy stderr",
        }
        script = (
            "import json, sys; "
            "sys.stderr.write('W' * 131072); sys.stderr.flush(); "
            f"open({result_out!r}, 'w').write({json.dumps(json.dumps(payload))})"
        )
        return orig_popen([sys.executable, "-c", script], **kwargs)

    t0 = time.monotonic()
    with patch("subprocess.Popen", side_effect=noisy_success_popen):
        res_noisy = preview.load_preview_isolated(str(pdf_path), wall_timeout=5.0)
    elapsed = time.monotonic() - t0
    assert res_noisy.kind == PreviewKind.METADATA
    assert res_noisy.note == "Recovered after noisy stderr"
    assert elapsed < 3.0

    # 2. Worker terminated by SIGKILL (-9) must return ERROR cleanly
    def killed_popen(cmd, **kwargs):
        return orig_popen(
            [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
            **kwargs,
        )

    with patch("subprocess.Popen", side_effect=killed_popen):
        res_killed = preview.load_preview_isolated(str(pdf_path), wall_timeout=5.0)
    assert res_killed.kind == PreviewKind.ERROR
    assert "signal 9" in (res_killed.error or "")


@pytest.mark.asyncio
async def test_preview_screen_survives_worker_render_and_image_widget_exceptions(tmp_path):
    pdf_path = tmp_path / "broken.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    # 1. Unhandled exception raised by isolated worker loader -> app stays alive, Esc returns
    app1 = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    with patch.object(preview, "load_preview_isolated", side_effect=RuntimeError("boom in worker")):
        async with app1.run_test(size=(120, 26)) as pilot:
            await _skip_boot(pilot)
            app1._panel("left").move_cursor_to("broken.pdf")
            await pilot.press("v")
            await pilot.pause(0.3)
            assert isinstance(app1.screen, PreviewScreen)
            meta_txt = str(app1.screen.query_one("#preview-meta", Static).content)
            assert "Preview worker error" in meta_txt
            await pilot.press("escape")
            await pilot.pause()
            assert app1.screen.__class__.__name__ == "MainScreen"

    # 2. Non-RuntimeError exception inside _render_result -> app stays alive, Esc returns
    app2 = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    fake_ok = preview.PreviewResult(
        kind=preview.PreviewKind.METADATA,
        path=str(pdf_path),
        metadata=preview._minimal_fallback_metadata(str(pdf_path)),
    )
    with (
        patch.object(preview, "load_preview_isolated", return_value=fake_ok),
        patch.object(PreviewScreen, "_render_result", side_effect=ValueError("boom in render")),
    ):
        async with app2.run_test(size=(120, 26)) as pilot:
            await _skip_boot(pilot)
            app2._panel("left").move_cursor_to("broken.pdf")
            await pilot.press("v")
            await pilot.pause(0.3)
            assert isinstance(app2.screen, PreviewScreen)
            meta_txt = str(app2.screen.query_one("#preview-meta", Static).content)
            assert "Preview display error" in meta_txt
            await pilot.press("escape")
            await pilot.pause()
            assert app2.screen.__class__.__name__ == "MainScreen"

    # 3. _build_image_widget failure in visual PDF mode -> falls back without crashing app
    if preview.is_image_engine_available():
        from PIL import Image as PILImage

        fake_img_res = preview.PreviewResult(
            kind=preview.PreviewKind.PDF,
            path=str(pdf_path),
            metadata=preview._minimal_fallback_metadata(str(pdf_path)),
            pil_image=PILImage.new("RGB", (20, 20), "red"),
            image_engine_available=True,
            pdf_visual_available=True,
            pdf_visual_mode=True,
            page_index=0,
            page_count=1,
            text="",
        )
        app3 = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
        with (
            patch.object(preview, "load_preview_isolated", return_value=fake_img_res),
            patch("textual_image.widget.Image", side_effect=ValueError("bad image widget")),
        ):
            async with app3.run_test(size=(120, 26)) as pilot:
                await _skip_boot(pilot)
                app3._panel("left").move_cursor_to("broken.pdf")
                await pilot.press("v")
                await pilot.pause(0.3)
                assert isinstance(app3.screen, PreviewScreen)
                assert app3.screen.query_one("#preview-text", TextArea) is not None
                await pilot.press("escape")
                await pilot.pause()
                assert app3.screen.__class__.__name__ == "MainScreen"
