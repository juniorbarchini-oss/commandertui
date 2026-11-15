"""Stage 2 document preview adapters (PDF, DOCX, XLSX, CSV/TSV) with strict bounds."""

from __future__ import annotations

import csv
import importlib.util
import io
import math
import os
import re
from typing import Any, Callable
import zipfile
import zlib

from .preview import (
    FileMetadata,
    PreviewCancelledError,
    PreviewKind,
    PreviewResult,
    _decode_text,
    _looks_binary,
    _read_bounded,
    format_size_bytes,
    is_image_engine_available,
    optional_engine_hint,
    sanitize_terminal_text,
)

MAX_DOC_BYTES = 25 * 1024 * 1024  # 25 MiB cap for PDF / DOCX / XLSX
MAX_OOXML_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # 50 MiB total decompressed limit
MAX_OOXML_ENTRIES = 2_000

MAX_DOCX_CHARS = 512 * 1024
MAX_DOCX_TABLES = 200

MAX_PDF_TEXT_CHARS = 200_000
MAX_PDF_RENDER_PIXELS = 25_000_000
MAX_PDF_RENDER_DIM = 1600

MAX_TABLE_ROWS = 500
MAX_TABLE_COLS = 50
MAX_TABLE_CELLS = 20_000
MAX_CELL_CHARS = 200

CSV_READ_BYTES = 512 * 1024
DOC_PREVIEW_TIMEOUT = 10.0

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_DANGEROUS_XML_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_ZIP_SCAN_CHUNK = 64 * 1024


def is_pdf_text_engine_available() -> bool:
    """Return True if optional `pypdf` package can be imported."""
    if os.environ.get("COMMANDERTUI_DISABLE_DOCUMENTS") == "1":
        return False
    return importlib.util.find_spec("pypdf") is not None


def is_pdf_render_engine_available() -> bool:
    """Return True if optional PDF rasterization (`pypdfium2`) and image engine are available."""
    if os.environ.get("COMMANDERTUI_DISABLE_PDF_RENDER") == "1":
        return False
    return importlib.util.find_spec("pypdfium2") is not None and is_image_engine_available()


def is_docx_engine_available() -> bool:
    """Return True if optional `python-docx` package can be imported."""
    if os.environ.get("COMMANDERTUI_DISABLE_DOCUMENTS") == "1":
        return False
    return importlib.util.find_spec("docx") is not None


def is_xlsx_engine_available() -> bool:
    """Return True if optional `openpyxl` package can be imported."""
    if os.environ.get("COMMANDERTUI_DISABLE_DOCUMENTS") == "1":
        return False
    return importlib.util.find_spec("openpyxl") is not None


def _col_letters(index: int) -> str:
    """Convert 0-based column index to Excel-style column name (A, B, ..., AA)."""
    result = ""
    num = index + 1
    while num > 0:
        num, rem = divmod(num - 1, 26)
        result = chr(65 + rem) + result
    return result


def _clean_cell(value: Any, *, max_chars: int = MAX_CELL_CHARS) -> str:
    if value is None:
        return ""
    text = sanitize_terminal_text(str(value)).replace("\r", " ").replace("\n", " ")
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def _abort_result(
    abs_path: str,
    meta: FileMetadata,
    deadline: float,
    timeout: float,
    cancel: Callable[[], bool] | None,
    *,
    kind_label: str = "Document",
) -> PreviewResult | None:
    import time

    if cancel and cancel():
        return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=meta, cancelled=True)
    if deadline - time.monotonic() <= 0:
        return PreviewResult(
            kind=PreviewKind.ERROR,
            path=abs_path,
            metadata=meta,
            error=f"{kind_label} preview timed out after {timeout:.1f}s.",
        )
    return None


def _validate_safe_ooxml(
    raw: bytes,
    *,
    max_uncompressed_bytes: int,
    max_entries: int,
    deadline: float,
    timeout: float,
    cancel: Callable[[], bool] | None,
    abs_path: str,
    meta: FileMetadata,
) -> PreviewResult | None:
    """Verify ZIP integrity, encryption absence, decompressed size cap, and XML DTD safety."""
    if raw.startswith(_OLE2_MAGIC):
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note="Encrypted or legacy binary Office container; showing metadata only.",
        )

    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            infos = zf.infolist()
            if len(infos) > max_entries:
                return PreviewResult(
                    kind=PreviewKind.METADATA,
                    path=abs_path,
                    metadata=meta,
                    note=f"Document archive exceeds entry limit ({len(infos):,} > {max_entries:,}).",
                )

            if any(info.flag_bits & 0x1 for info in infos):
                return PreviewResult(
                    kind=PreviewKind.METADATA,
                    path=abs_path,
                    metadata=meta,
                    note="Encrypted / password-protected document; showing metadata only.",
                )

            declared_total = sum(max(0, info.file_size) for info in infos)
            if declared_total > max_uncompressed_bytes:
                return PreviewResult(
                    kind=PreviewKind.METADATA,
                    path=abs_path,
                    metadata=meta,
                    note=(
                        f"Document exceeds decompressed size limit "
                        f"({format_size_bytes(declared_total)} > {format_size_bytes(max_uncompressed_bytes)})."
                    ),
                )

            actual_total = 0
            for info in infos:
                abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="Document")
                if abort is not None:
                    return abort
                if info.is_dir():
                    continue
                lower_name = info.filename.lower()
                is_xml_like = lower_name.endswith((".xml", ".rels"))
                overlap = b""
                with zf.open(info, "r") as member:
                    while True:
                        abort = _abort_result(
                            abs_path, meta, deadline, timeout, cancel, kind_label="Document"
                        )
                        if abort is not None:
                            return abort
                        chunk = member.read(_ZIP_SCAN_CHUNK)
                        if not chunk:
                            break
                        actual_total += len(chunk)
                        if actual_total > max_uncompressed_bytes:
                            return PreviewResult(
                                kind=PreviewKind.METADATA,
                                path=abs_path,
                                metadata=meta,
                                note=(
                                    f"Document exceeds decompressed size limit "
                                    f"(> {format_size_bytes(max_uncompressed_bytes)})."
                                ),
                            )
                        if is_xml_like:
                            window = overlap + chunk
                            if _DANGEROUS_XML_RE.search(window):
                                return PreviewResult(
                                    kind=PreviewKind.METADATA,
                                    path=abs_path,
                                    metadata=meta,
                                    note="Unsafe XML DTD/entity declaration rejected; showing metadata only.",
                                )
                            overlap = window[-16:]
    except (zipfile.BadZipFile, zlib.error, OSError, ValueError, RuntimeError) as exc:
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=f"Corrupted or unreadable document archive: {sanitize_terminal_text(str(exc))}",
        )
    return None


def _read_bounded_doc(
    abs_path: str,
    meta: FileMetadata,
    st: os.stat_result,
    byte_limit: int,
    timeout: float,
    cancel: Callable[[], bool] | None,
    *,
    kind_label: str,
) -> tuple[bytes | None, PreviewResult | None]:
    if st.st_size == 0:
        return None, PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=f"Empty {kind_label} file (0 bytes).",
        )
    if st.st_size > byte_limit:
        return None, PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=(
                f"{kind_label} exceeds preview byte limit "
                f"({format_size_bytes(st.st_size)} > {format_size_bytes(byte_limit)})."
            ),
        )
    try:
        raw, truncated = _read_bounded(abs_path, byte_limit, timeout, cancel)
    except PreviewCancelledError:
        return None, PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=meta, cancelled=True)
    except OSError as exc:
        return None, PreviewResult(
            kind=PreviewKind.ERROR,
            path=abs_path,
            metadata=meta,
            error=sanitize_terminal_text(str(exc)),
        )
    if truncated is not None:
        return None, PreviewResult(
            kind=PreviewKind.ERROR if "timed out" in truncated.lower() else PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            error=truncated if "timed out" in truncated.lower() else None,
            note=None if "timed out" in truncated.lower() else f"{kind_label} preview skipped: {truncated}.",
        )
    return raw, None


# ---------------------------------------------------------------------------
# CSV / TSV adapter (stdlib)
# ---------------------------------------------------------------------------


_CSV_DELIMITER_CANDIDATES = (",", ";", "\t", "|")


def _detect_csv_delimiter(text: str) -> str:
    """Pick the delimiter that splits the first lines into the most columns (comma on ties).

    Spreadsheet exports in many locales (Spanish among them) use ';' in `.csv` files.
    """
    sample = [line for line in text.splitlines()[:20] if line.strip()][:10]
    best, best_cols = ",", 1
    for cand in _CSV_DELIMITER_CANDIDATES:
        try:
            cols = min((len(row) for row in csv.reader(sample, delimiter=cand)), default=1)
        except csv.Error:
            continue
        if cols > best_cols:
            best, best_cols = cand, cols
    return best


def load_csv_preview(
    abs_path: str,
    meta: FileMetadata,
    st: os.stat_result,
    cancel: Callable[[], bool] | None,
    *,
    delimiter: str | None = ",",
    byte_limit: int = CSV_READ_BYTES,
    max_rows: int = MAX_TABLE_ROWS,
    max_cols: int = MAX_TABLE_COLS,
    max_cells: int = MAX_TABLE_CELLS,
    timeout: float = DOC_PREVIEW_TIMEOUT,
) -> PreviewResult:
    import time

    deadline = time.monotonic() + timeout
    if st.st_size == 0:
        return PreviewResult(
            kind=PreviewKind.TABLE,
            path=abs_path,
            metadata=meta,
            table_headers=["(empty)"],
            table_rows=[],
            encoding="utf-8",
            note="Empty table file (0 bytes).",
        )

    try:
        raw, truncated = _read_bounded(abs_path, byte_limit, max(0.0, deadline - time.monotonic()), cancel)
    except PreviewCancelledError:
        return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=meta, cancelled=True)
    except OSError as exc:
        return PreviewResult(
            kind=PreviewKind.ERROR,
            path=abs_path,
            metadata=meta,
            error=sanitize_terminal_text(str(exc)),
        )

    abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="Table")
    if abort is not None:
        return abort

    if _looks_binary(raw[:8192]):
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note="Binary content detected; showing metadata only.",
        )

    text, encoding = _decode_text(raw, truncated=truncated is not None or st.st_size > len(raw))
    if truncated is not None and "\n" in text:
        # Drop partial last line when truncated by bytes
        text = text.rsplit("\n", 1)[0]
    if delimiter is None:
        delimiter = _detect_csv_delimiter(text)

    max_cells = max(1, max_cells)
    parsed_rows: list[list[str]] = []
    cols_truncated = False
    rows_truncated = False
    cells_truncated = False

    orig_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(min(orig_limit, 256 * 1024))
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        for row_idx, raw_row in enumerate(reader):
            abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="Table")
            if abort is not None:
                return abort
            if len(raw_row) > max_cols:
                cols_truncated = True
                raw_row = raw_row[:max_cols]
            if row_idx > max_rows:
                rows_truncated = True
                break
            parsed_rows.append([_clean_cell(c) for c in raw_row])
    except csv.Error as exc:
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=f"Could not parse delimiter-separated table: {sanitize_terminal_text(str(exc))}",
        )
    finally:
        csv.field_size_limit(orig_limit)

    if not parsed_rows:
        return PreviewResult(
            kind=PreviewKind.TABLE,
            path=abs_path,
            metadata=meta,
            table_headers=["(empty)"],
            table_rows=[],
            encoding=encoding,
            note="No rows found in table.",
        )

    # Enforce rectangular cell cap across header + data rows so padding cannot exceed max_cells.
    rect_w = 0
    rows: list[list[str]] = []
    for idx, r in enumerate(parsed_rows):
        eff_w = len(r)
        while eff_w > 0 and r[eff_w - 1] == "":
            eff_w -= 1
        candidate_w = max(rect_w, max(1, eff_w) if idx == 0 else eff_w)
        # Rendered table has 1 header row (when no data rows) or len(data_rows) * col_count
        # Using (len(rows) + 1) * max(1, candidate_w) bounds both headers + data cells strictly.
        if (len(rows) + 1) * max(1, candidate_w) > max_cells:
            if not rows:
                cap = max(1, min(len(r), max_cells))
                rows.append(r[:cap])
                rect_w = len(rows[0])
                cols_truncated = True
            cells_truncated = True
            break
        rows.append(r)
        rect_w = candidate_w

    width = max(1, min(rect_w, max_cols))
    raw_headers = rows[0]
    headers: list[str] = []
    for idx in range(width):
        h = raw_headers[idx] if idx < len(raw_headers) else ""
        headers.append(h or f"Col {idx + 1}")

    data_rows: list[list[str]] = []
    for r in rows[1:]:
        padded = r[:width] + [""] * max(0, width - len(r))
        data_rows.append(padded)

    reasons: list[str] = []
    if truncated is not None:
        reasons.append(truncated)
    elif st.st_size > len(raw):
        reasons.append(f"Truncated at {format_size_bytes(byte_limit)}")
    if rows_truncated:
        reasons.append(f"Showing first {max_rows} rows")
    if cols_truncated:
        reasons.append(f"Columns capped at {max_cols}")
    if cells_truncated:
        reasons.append(f"Cell cap reached ({max_cells:,})")

    return PreviewResult(
        kind=PreviewKind.TABLE,
        path=abs_path,
        metadata=meta,
        encoding=encoding,
        table_headers=headers,
        table_rows=data_rows,
        truncated_reason="; ".join(reasons) if reasons else None,
    )


# ---------------------------------------------------------------------------
# PDF adapter (pypdf selectable text + optional pypdfium2 visual render)
# ---------------------------------------------------------------------------


def _extract_pdf_text_pdfium(
    raw: bytes,
    page_index: int,
) -> tuple[str | None, int, int, str | None]:
    import pypdfium2

    doc = None
    page = None
    textpage = None
    try:
        doc = pypdfium2.PdfDocument(raw)
        page_count = len(doc)
        if page_count <= 0:
            return None, 0, 0, "PDF contains 0 pages."
        clamped = max(0, min(page_index, page_count - 1))
        page = doc[clamped]
        textpage = page.get_textpage()
        extracted = textpage.get_text_range() or ""
        return extracted, clamped, page_count, None
    except Exception as exc:
        return None, 0, 0, f"Could not parse PDF: {sanitize_terminal_text(str(exc))}"
    finally:
        for handle in (textpage, page, doc):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass


def _render_pdf_page_image(
    raw: bytes,
    page_index: int,
    *,
    pixel_limit: int,
    max_dim: int,
) -> tuple[Any | None, int, int, tuple[int, int] | None, str | None]:
    import pypdfium2

    doc = None
    page = None
    bitmap = None
    try:
        doc = pypdfium2.PdfDocument(raw)
        page_count = len(doc)
        if page_count <= 0:
            return None, 0, 0, None, "PDF contains 0 pages."
        clamped = max(0, min(page_index, page_count - 1))
        page = doc[clamped]
        raw_w, raw_h = page.get_size()
        w_pt = float(raw_w)
        h_pt = float(raw_h)
        if not math.isfinite(w_pt) or not math.isfinite(h_pt) or w_pt <= 0 or h_pt <= 0:
            return None, clamped, page_count, None, "Invalid PDF page dimensions."

        scale = min(2.0, max_dim / max(w_pt, h_pt))
        est_pixels = (w_pt * scale) * (h_pt * scale)
        if est_pixels > pixel_limit:
            scale *= (pixel_limit / est_pixels) ** 0.5

        target_w = math.ceil(w_pt * scale)
        target_h = math.ceil(h_pt * scale)
        while scale > 0 and (target_w > max_dim or target_h > max_dim or target_w * target_h > pixel_limit):
            scale *= 0.99
            target_w = math.ceil(w_pt * scale)
            target_h = math.ceil(h_pt * scale)

        if (
            not math.isfinite(scale)
            or scale <= 0
            or (w_pt * scale) < 1.0
            or (h_pt * scale) < 1.0
            or target_w < 1
            or target_h < 1
            or target_w > max_dim
            or target_h > max_dim
            or (target_w * target_h) > pixel_limit
        ):
            return (
                None,
                clamped,
                page_count,
                None,
                "PDF page size or aspect ratio exceeds safe render scale bounds.",
            )

        bitmap = page.render(scale=scale, may_draw_forms=False)
        pil_img = bitmap.to_pil()
        if pil_img.width <= 0 or pil_img.height <= 0:
            return None, clamped, page_count, None, "Rendered PDF bitmap is empty."
        if pil_img.mode not in ("RGB", "RGBA"):
            pil_img = pil_img.convert("RGB")
        else:
            pil_img = pil_img.copy()
        if (
            pil_img.width > max_dim
            or pil_img.height > max_dim
            or pil_img.width * pil_img.height > pixel_limit
        ):
            pil_img.thumbnail((max_dim, max_dim))
        pil_img.load()
        return pil_img, clamped, page_count, pil_img.size, None
    except Exception as exc:
        return None, 0, 0, None, f"Could not render PDF page: {sanitize_terminal_text(str(exc))}"
    finally:
        for handle in (bitmap, page, doc):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass


def _extract_pdf_text_result(
    raw: bytes,
    abs_path: str,
    meta: FileMetadata,
    page_index: int,
    deadline: float,
    timeout: float,
    cancel: Callable[[], bool] | None,
    *,
    max_chars: int,
    text_engine_ok: bool,
    visual_engine_ok: bool,
    extra_note: str | None = None,
) -> PreviewResult:
    extracted = ""
    clamped = 0
    page_count = 0

    if text_engine_ok:
        try:
            import pypdf
            from pypdf import errors as pypdf_errors
        except ImportError:
            text_engine_ok = False
        else:
            try:
                reader = pypdf.PdfReader(io.BytesIO(raw), strict=False)
                if reader.is_encrypted:
                    try:
                        decrypted = reader.decrypt("")
                    except Exception:
                        decrypted = 0
                    if not decrypted:
                        return PreviewResult(
                            kind=PreviewKind.METADATA,
                            path=abs_path,
                            metadata=meta,
                            pdf_visual_available=visual_engine_ok,
                            note="Encrypted / password-protected PDF; showing metadata only.",
                        )
                page_count = len(reader.pages)
                if page_count == 0:
                    return PreviewResult(
                        kind=PreviewKind.METADATA,
                        path=abs_path,
                        metadata=meta,
                        pdf_visual_available=visual_engine_ok,
                        note="PDF contains 0 pages.",
                    )
                abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="PDF")
                if abort is not None:
                    return abort
                clamped = max(0, min(page_index, page_count - 1))
                extracted = reader.pages[clamped].extract_text() or ""
            except Exception as exc:
                return PreviewResult(
                    kind=PreviewKind.METADATA,
                    path=abs_path,
                    metadata=meta,
                    pdf_visual_available=visual_engine_ok,
                    note=f"Could not parse PDF: {sanitize_terminal_text(str(exc))}",
                )

    if not text_engine_ok:
        if not visual_engine_ok:
            return PreviewResult(
                kind=PreviewKind.METADATA,
                path=abs_path,
                metadata=meta,
                pdf_visual_available=False,
                note=f"Optional PDF preview engine not installed ({optional_engine_hint('documents')}).",
            )
        pdfium_text, clamped, page_count, err = _extract_pdf_text_pdfium(raw, page_index)
        if err is not None or pdfium_text is None:
            return PreviewResult(
                kind=PreviewKind.METADATA,
                path=abs_path,
                metadata=meta,
                pdf_visual_available=visual_engine_ok,
                note=err or "Could not parse PDF.",
            )
        extracted = pdfium_text

    abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="PDF")
    if abort is not None:
        return abort

    sanitized = sanitize_terminal_text(extracted)
    truncated_reason = None
    if len(sanitized) > max_chars:
        sanitized = sanitized[:max_chars]
        truncated_reason = f"Page text truncated at {max_chars:,} characters"

    notes: list[str] = []
    if extra_note:
        notes.append(extra_note)
    if not sanitized.strip() and not extra_note:
        if visual_engine_ok:
            notes.append("No selectable text found on this page (press g for visual page preview).")
        else:
            notes.append(
                "No selectable text found on this page; visual page preview is unavailable "
                f"({optional_engine_hint('pdf-render')})."
            )

    return PreviewResult(
        kind=PreviewKind.PDF,
        path=abs_path,
        metadata=meta,
        text=sanitized,
        truncated_reason=truncated_reason,
        encoding="utf-8",
        page_index=clamped,
        page_count=page_count,
        pdf_visual_available=visual_engine_ok,
        pdf_visual_mode=False,
        image_engine_available=visual_engine_ok,
        note=" | ".join(notes) if notes else None,
    )


def load_pdf_preview(
    abs_path: str,
    meta: FileMetadata,
    st: os.stat_result,
    cancel: Callable[[], bool] | None,
    *,
    page_index: int = 0,
    render_visual: bool = False,
    byte_limit: int = MAX_DOC_BYTES,
    max_chars: int = MAX_PDF_TEXT_CHARS,
    pixel_limit: int = MAX_PDF_RENDER_PIXELS,
    max_dim: int = MAX_PDF_RENDER_DIM,
    timeout: float = DOC_PREVIEW_TIMEOUT,
) -> PreviewResult:
    import time

    deadline = time.monotonic() + timeout
    text_engine_ok = is_pdf_text_engine_available()
    visual_engine_ok = is_pdf_render_engine_available()

    if not text_engine_ok and not visual_engine_ok:
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            pdf_visual_available=False,
            image_engine_available=False,
            note=f"Optional PDF preview engine not installed ({optional_engine_hint('documents')}).",
        )

    raw, err_res = _read_bounded_doc(
        abs_path, meta, st, byte_limit, max(0.0, deadline - time.monotonic()), cancel, kind_label="PDF"
    )
    if err_res is not None or raw is None:
        return err_res  # type: ignore[return-value]

    abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="PDF")
    if abort is not None:
        return abort

    if render_visual:
        if not visual_engine_ok:
            return _extract_pdf_text_result(
                raw,
                abs_path,
                meta,
                page_index,
                deadline,
                timeout,
                cancel,
                max_chars=max_chars,
                text_engine_ok=text_engine_ok,
                visual_engine_ok=False,
                extra_note=f"Optional visual PDF renderer not installed ({optional_engine_hint('pdf-render')}).",
            )
        pil_img, clamped, page_count, dims, render_err = _render_pdf_page_image(
            raw, page_index, pixel_limit=pixel_limit, max_dim=max_dim
        )
        abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="PDF")
        if abort is not None:
            return abort
        if render_err is not None or pil_img is None:
            if page_count > 0:
                return _extract_pdf_text_result(
                    raw,
                    abs_path,
                    meta,
                    clamped,
                    deadline,
                    timeout,
                    cancel,
                    max_chars=max_chars,
                    text_engine_ok=text_engine_ok,
                    visual_engine_ok=True,
                    extra_note=f"{render_err or 'Could not render PDF page.'} Showing text view.",
                )
            return PreviewResult(
                kind=PreviewKind.METADATA,
                path=abs_path,
                metadata=meta,
                page_index=clamped,
                page_count=page_count,
                pdf_visual_available=True,
                pdf_visual_mode=False,
                image_engine_available=True,
                note=render_err or "Could not render PDF page.",
            )
        meta.image_dimensions = dims
        return PreviewResult(
            kind=PreviewKind.PDF,
            path=abs_path,
            metadata=meta,
            pil_image=pil_img,
            image_engine_available=True,
            page_index=clamped,
            page_count=page_count,
            pdf_visual_available=True,
            pdf_visual_mode=True,
        )

    return _extract_pdf_text_result(
        raw,
        abs_path,
        meta,
        page_index,
        deadline,
        timeout,
        cancel,
        max_chars=max_chars,
        text_engine_ok=text_engine_ok,
        visual_engine_ok=visual_engine_ok,
    )


# ---------------------------------------------------------------------------
# DOCX adapter (python-docx paragraphs + tables in document order)
# ---------------------------------------------------------------------------


def _format_docx_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(r) for r in rows)
    if width <= 0:
        return []
    col_widths = [max((len(r[i]) if i < len(r) else 0) for r in rows) for i in range(width)]
    col_widths = [max(3, min(w, 40)) for w in col_widths]

    def _fmt(row: list[str]) -> str:
        cells = []
        for idx, cw in enumerate(col_widths):
            val = row[idx] if idx < len(row) else ""
            if len(val) > cw:
                val = val[: cw - 1] + "…"
            cells.append(val.ljust(cw))
        return "| " + " | ".join(cells) + " |"

    lines = [_fmt(rows[0])]
    lines.append("| " + " | ".join("-" * cw for cw in col_widths) + " |")
    for r in rows[1:]:
        lines.append(_fmt(r))
    return lines


def load_docx_preview(
    abs_path: str,
    meta: FileMetadata,
    st: os.stat_result,
    cancel: Callable[[], bool] | None,
    *,
    byte_limit: int = MAX_DOC_BYTES,
    max_uncompressed: int = MAX_OOXML_UNCOMPRESSED_BYTES,
    max_chars: int = MAX_DOCX_CHARS,
    timeout: float = DOC_PREVIEW_TIMEOUT,
) -> PreviewResult:
    import time

    deadline = time.monotonic() + timeout
    if not is_docx_engine_available():
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=f"Optional DOCX preview engine not installed ({optional_engine_hint('documents')}).",
        )

    raw, err_res = _read_bounded_doc(
        abs_path, meta, st, byte_limit, max(0.0, deadline - time.monotonic()), cancel, kind_label="DOCX"
    )
    if err_res is not None or raw is None:
        return err_res  # type: ignore[return-value]

    unsafe = _validate_safe_ooxml(
        raw,
        max_uncompressed_bytes=max_uncompressed,
        max_entries=MAX_OOXML_ENTRIES,
        deadline=deadline,
        timeout=timeout,
        cancel=cancel,
        abs_path=abs_path,
        meta=meta,
    )
    if unsafe is not None:
        return unsafe

    try:
        import docx
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=f"Optional DOCX preview engine not installed ({optional_engine_hint('documents')}).",
        )

    try:
        document = docx.Document(io.BytesIO(raw))
        out_blocks: list[str] = []
        total_chars = 0
        table_count = 0
        truncated_reason: str | None = None

        def _append_block(block: str) -> bool:
            nonlocal total_chars, truncated_reason
            if not block:
                return True
            if total_chars + len(block) > max_chars:
                remaining = max(0, max_chars - total_chars)
                if remaining > 0:
                    out_blocks.append(block[:remaining])
                truncated_reason = f"Document text truncated at {format_size_bytes(max_chars)}"
                return False
            out_blocks.append(block)
            total_chars += len(block) + 2
            return True

        def _walk(container) -> bool:
            nonlocal table_count, truncated_reason
            for child in container.iterchildren():
                abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="DOCX")
                if abort is not None:
                    raise TimeoutError("aborted")
                if isinstance(child, CT_P):
                    para = Paragraph(child, document)
                    text = sanitize_terminal_text(para.text).strip()
                    if not text:
                        continue
                    style_name = (para.style.name if para.style and para.style.name else "").lower()
                    if style_name.startswith("heading"):
                        prefix = "#"
                        for ch in style_name:
                            if ch.isdigit():
                                prefix = "#" * max(1, min(4, int(ch)))
                                break
                        text = f"{prefix} {text}"
                    if not _append_block(text):
                        return False
                elif isinstance(child, CT_Tbl):
                    table_count += 1
                    if table_count > MAX_DOCX_TABLES:
                        truncated_reason = f"Document truncated at {MAX_DOCX_TABLES} tables"
                        return False
                    tbl = Table(child, document)
                    tbl_rows: list[list[str]] = []
                    for r_idx, row in enumerate(tbl.rows):
                        if r_idx >= MAX_TABLE_ROWS:
                            truncated_reason = f"Table truncated at {MAX_TABLE_ROWS} rows"
                            break
                        row_cells: list[str] = []
                        for c_idx, cell in enumerate(row.cells):
                            if c_idx >= MAX_TABLE_COLS:
                                break
                            row_cells.append(_clean_cell(cell.text))
                        tbl_rows.append(row_cells)
                    formatted = "\n".join(_format_docx_table(tbl_rows))
                    if not _append_block(formatted):
                        return False
                elif child.tag.endswith("}sdt"):
                    for sub in child:
                        if sub.tag.endswith("}sdtContent"):
                            if not _walk(sub):
                                return False
            return True

        _walk(document.element.body)
        abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="DOCX")
        if abort is not None:
            return abort

        rendered = "\n\n".join(out_blocks)
        note = None if rendered.strip() else "DOCX document contains no extractable text or tables."
        return PreviewResult(
            kind=PreviewKind.TEXT,
            path=abs_path,
            metadata=meta,
            text=rendered,
            truncated_reason=truncated_reason,
            encoding="utf-8",
            note=note,
        )
    except TimeoutError:
        abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="DOCX")
        if abort is not None:
            return abort
        return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=meta, error="DOCX timed out.")
    except Exception as exc:
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=f"Could not parse DOCX document: {sanitize_terminal_text(str(exc))}",
        )


# ---------------------------------------------------------------------------
# XLSX adapter (openpyxl read_only + data_only, no formula/macro execution)
# ---------------------------------------------------------------------------


def load_xlsx_preview(
    abs_path: str,
    meta: FileMetadata,
    st: os.stat_result,
    cancel: Callable[[], bool] | None,
    *,
    sheet_index: int = 0,
    byte_limit: int = MAX_DOC_BYTES,
    max_uncompressed: int = MAX_OOXML_UNCOMPRESSED_BYTES,
    max_rows: int = MAX_TABLE_ROWS,
    max_cols: int = MAX_TABLE_COLS,
    max_cells: int = MAX_TABLE_CELLS,
    timeout: float = DOC_PREVIEW_TIMEOUT,
) -> PreviewResult:
    import time

    deadline = time.monotonic() + timeout
    if not is_xlsx_engine_available():
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=f"Optional XLSX preview engine not installed ({optional_engine_hint('documents')}).",
        )

    raw, err_res = _read_bounded_doc(
        abs_path, meta, st, byte_limit, max(0.0, deadline - time.monotonic()), cancel, kind_label="XLSX"
    )
    if err_res is not None or raw is None:
        return err_res  # type: ignore[return-value]

    unsafe = _validate_safe_ooxml(
        raw,
        max_uncompressed_bytes=max_uncompressed,
        max_entries=MAX_OOXML_ENTRIES,
        deadline=deadline,
        timeout=timeout,
        cancel=cancel,
        abs_path=abs_path,
        meta=meta,
    )
    if unsafe is not None:
        return unsafe

    try:
        import openpyxl
    except ImportError:
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=f"Optional XLSX preview engine not installed ({optional_engine_hint('documents')}).",
        )

    wb_vals = None
    wb_formulas = None
    sheet_names: list[str] = []
    clamped_sheet = 0
    try:
        wb_vals = openpyxl.load_workbook(
            io.BytesIO(raw), read_only=True, data_only=True, keep_vba=False, keep_links=False
        )
        sheet_names = [sanitize_terminal_text(name) for name in wb_vals.sheetnames]
        if not sheet_names:
            return PreviewResult(
                kind=PreviewKind.METADATA,
                path=abs_path,
                metadata=meta,
                note="Workbook contains 0 sheets.",
            )

        clamped_sheet = max(0, min(sheet_index, len(wb_vals.sheetnames) - 1))
        target_name = wb_vals.sheetnames[clamped_sheet]
        ws_vals = wb_vals[target_name]
        if not hasattr(ws_vals, "iter_rows"):
            return PreviewResult(
                kind=PreviewKind.TABLE,
                path=abs_path,
                metadata=meta,
                table_headers=["(chart)"],
                table_rows=[],
                sheet_names=sheet_names,
                sheet_index=clamped_sheet,
                encoding="utf-8",
                note=f'Sheet "{sheet_names[clamped_sheet]}" is a chart sheet; it has no cells to show.',
            )

        max_cells = max(1, max_cells)
        rows_raw: list[list[Any]] = []
        rows_truncated = False
        cols_truncated = bool((ws_vals.max_column or 0) > max_cols)
        cells_truncated = False
        has_none_cells = False

        for r_idx, row in enumerate(
            ws_vals.iter_rows(min_row=1, max_row=max_rows + 1, max_col=max_cols + 1, values_only=True)
        ):
            abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="XLSX")
            if abort is not None:
                return abort
            if r_idx >= max_rows:
                rows_truncated = True
                break
            row_vals = list(row)
            if len(row_vals) > max_cols:
                if any(v is not None for v in row_vals[max_cols:]):
                    cols_truncated = True
                row_vals = row_vals[:max_cols]
            if any(v is None for v in row_vals):
                has_none_cells = True
            rows_raw.append(row_vals)

        if (ws_vals.max_row or 0) > max_rows and len(rows_raw) >= max_rows:
            rows_truncated = True

        # If a workbook stored formulas without cached values, fall back cell-by-cell to the
        # literal formula text as data without evaluating formulas (bounded to the same window).
        if has_none_cells and rows_raw:
            wb_formulas = openpyxl.load_workbook(
                io.BytesIO(raw), read_only=True, data_only=False, keep_vba=False, keep_links=False
            )
            ws_f = wb_formulas[target_name]
            if (ws_f.max_column or 0) > max_cols:
                cols_truncated = True
            for r_idx, f_row in enumerate(
                ws_f.iter_rows(min_row=1, max_row=len(rows_raw), max_col=max_cols + 1, values_only=True)
            ):
                if r_idx >= len(rows_raw):
                    break
                abort = _abort_result(abs_path, meta, deadline, timeout, cancel, kind_label="XLSX")
                if abort is not None:
                    return abort
                if len(f_row) > max_cols and any(v is not None for v in f_row[max_cols:]):
                    cols_truncated = True
                target_row = rows_raw[r_idx]
                f_slice = list(f_row[:max_cols])
                if len(f_slice) > len(target_row):
                    target_row.extend([None] * (len(f_slice) - len(target_row)))
                for c_idx, f_val in enumerate(f_slice):
                    if target_row[c_idx] is None and f_val is not None:
                        target_row[c_idx] = f_val

        # Trim trailing None cells per row and drop trailing completely-empty rows
        trimmed_rows: list[list[Any]] = []
        for row_vals in rows_raw:
            row_copy = list(row_vals)
            while row_copy and row_copy[-1] is None:
                row_copy.pop()
            trimmed_rows.append(row_copy)
        while trimmed_rows and not trimmed_rows[-1]:
            trimmed_rows.pop()

        # Enforce rectangular cell cap on the actual padded table (rows * cols <= max_cells)
        rect_w = 0
        kept_rows: list[list[Any]] = []
        for row_vals in trimmed_rows:
            candidate_w = max(rect_w, len(row_vals))
            if (len(kept_rows) + 1) * max(1, candidate_w) > max_cells:
                if not kept_rows:
                    cap = max(1, min(len(row_vals), max_cells))
                    kept_rows.append(row_vals[:cap])
                    rect_w = len(kept_rows[0])
                    cols_truncated = True
                cells_truncated = True
                break
            kept_rows.append(row_vals)
            rect_w = candidate_w

        col_count = max(1, min(rect_w, max_cols)) if kept_rows else 1
        headers = [_col_letters(i) for i in range(col_count)]
        table_rows: list[list[str]] = []
        for row_vals in kept_rows:
            cleaned = [_clean_cell(row_vals[i] if i < len(row_vals) else None) for i in range(col_count)]
            table_rows.append(cleaned)

        reasons: list[str] = []
        if rows_truncated:
            reasons.append(f"Showing first {max_rows} rows")
        if cols_truncated:
            reasons.append(f"Columns capped at {max_cols}")
        if cells_truncated:
            reasons.append(f"Cell cap reached ({max_cells:,})")

        note = None if table_rows else f'Sheet "{sheet_names[clamped_sheet]}" is empty.'
        return PreviewResult(
            kind=PreviewKind.TABLE,
            path=abs_path,
            metadata=meta,
            table_headers=headers,
            table_rows=table_rows,
            sheet_names=sheet_names,
            sheet_index=clamped_sheet,
            encoding="utf-8",
            truncated_reason="; ".join(reasons) if reasons else None,
            note=note,
        )
    except Exception as exc:
        if len(sheet_names) > 1:
            return PreviewResult(
                kind=PreviewKind.TABLE,
                path=abs_path,
                metadata=meta,
                table_headers=["(unreadable)"],
                table_rows=[],
                sheet_names=sheet_names,
                sheet_index=clamped_sheet,
                encoding="utf-8",
                note=(
                    f'Could not read sheet "{sheet_names[clamped_sheet]}": '
                    f"{sanitize_terminal_text(str(exc))}"
                ),
            )
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=f"Could not parse XLSX workbook: {sanitize_terminal_text(str(exc))}",
        )
    finally:
        for wb in (wb_formulas, wb_vals):
            if wb is not None:
                try:
                    wb.close()
                except Exception:
                    pass
