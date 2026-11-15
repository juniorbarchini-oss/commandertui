"""Cross-platform file preview loader and macOS Quick Look helper."""

from __future__ import annotations

import atexit
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import importlib.util
import io
import json
import mimetypes
import os
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
import warnings

# ---------------------------------------------------------------------------
# Limits and format tables
# ---------------------------------------------------------------------------

MAX_TEXT_BYTES = 512 * 1024  # 512 KiB
MAX_TEXT_LINES = 10_000
TEXT_READ_TIMEOUT = 5.0

MAX_IMAGE_BYTES = 25 * 1024 * 1024  # 25 MiB
MAX_IMAGE_PIXELS = 25_000_000  # 25 MP
MAX_IMAGE_DIM = 1600
IMAGE_READ_TIMEOUT = 10.0

READ_CHUNK_SIZE = 64 * 1024
SNIFF_BYTES = 8 * 1024
ISOLATED_PREVIEW_GRACE_SECONDS = 2.0

MARKDOWN_EXTS = frozenset({".md", ".markdown", ".mdown", ".mkd", ".mkdn"})
IMAGE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico", ".ppm", ".pgm", ".pbm"}
)
PDF_EXTS = frozenset({".pdf"})
DOCX_EXTS = frozenset({".docx"})
XLSX_EXTS = frozenset({".xlsx"})
CSV_EXTS = frozenset({".csv", ".tsv"})
LEGACY_OFFICE_EXTS = frozenset(
    {".doc", ".xls", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".docm", ".xlsm", ".pptm"}
)
VIDEO_EXTS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".m4v", ".mpg", ".mpeg"})
AUDIO_EXTS = frozenset({".mp3", ".flac", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".wma"})
ARCHIVE_EXTS = frozenset({".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst"})

TEXT_EXTS = frozenset(
    {
        ".txt",
        ".log",
        ".py",
        ".pyi",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sql",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".rb",
        ".php",
        ".lua",
        ".r",
        ".env",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".dockerfile",
    }
)
TEXT_FILENAMES = frozenset({"readme", "license", "makefile", "dockerfile", "changelog", "authors", "notice"})


class PreviewKind(Enum):
    TEXT = "text"
    MARKDOWN = "markdown"
    IMAGE = "image"
    PDF = "pdf"
    TABLE = "table"
    METADATA = "metadata"
    ERROR = "error"


@dataclass
class FileMetadata:
    name: str
    path: str
    size_bytes: int | None
    mtime: float | None
    ctime: float | None
    mode_str: str
    octal_mode: str
    is_symlink: bool
    symlink_target: str | None
    file_type: str
    mime_type: str
    image_dimensions: tuple[int, int] | None = None


@dataclass
class PreviewResult:
    kind: PreviewKind
    path: str
    metadata: FileMetadata
    text: str | None = None
    truncated_reason: str | None = None
    encoding: str | None = None
    pil_image: Any | None = None
    image_engine_available: bool = False
    page_index: int = 0
    page_count: int = 0
    pdf_visual_available: bool = False
    pdf_visual_mode: bool = False
    table_headers: list[str] | None = None
    table_rows: list[list[str]] | None = None
    sheet_names: list[str] | None = None
    sheet_index: int = 0
    note: str | None = None
    error: str | None = None
    cancelled: bool = False


class PreviewCancelledError(Exception):
    """Raised internally when a background preview load is cancelled."""


# ---------------------------------------------------------------------------
# macOS Quick Look helper
# ---------------------------------------------------------------------------

_current_process: subprocess.Popen | None = None
_current_path: str | None = None


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_active() -> bool:
    """Return True if a preview process is currently running."""
    global _current_process, _current_path
    if _current_process is None:
        return False
    if _current_process.poll() is not None:
        _current_process = None
        _current_path = None
        return False
    return True


def current_path() -> str | None:
    """Return the absolute path of the file currently being previewed, if any."""
    if is_active():
        return _current_path
    return None


def close_preview() -> None:
    """Terminate the active Quick Look preview process, if any."""
    global _current_process, _current_path
    if _current_process is not None:
        if _current_process.poll() is None:
            try:
                _current_process.terminate()
                try:
                    _current_process.wait(timeout=0.5)
                except (subprocess.TimeoutExpired, OSError):
                    _current_process.kill()
            except OSError:
                pass
        _current_process = None
        _current_path = None


atexit.register(close_preview)


def _bring_to_front_worker(proc: subprocess.Popen) -> None:
    """Wait for qlmanage to appear in System Events and bring its window to front."""
    script = 'tell application "System Events" to set frontmost of (first process whose name is "qlmanage") to true'
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if proc.poll() is not None or _current_process is not proc:
            return
        try:
            res = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3.0,
            )
            if res.returncode == 0:
                return
        except Exception:
            return
        time.sleep(0.1)


def _bring_to_front(proc: subprocess.Popen) -> threading.Thread:
    thread = threading.Thread(target=_bring_to_front_worker, args=(proc,), daemon=True)
    thread.start()
    return thread


def toggle_preview(path: str) -> bool:
    """Toggle Quick Look preview for `path`.

    If preview for `path` is already open, closes it.
    If preview for another file is open, closes it and opens `path`.
    Returns True if preview was started, False if it was closed or cannot run.
    """
    global _current_process, _current_path
    if not is_macos():
        return False

    abs_path = os.path.abspath(path)

    # If already open for this exact file, close it
    if is_active() and _current_path == abs_path:
        close_preview()
        return False

    # Close previous file preview if different
    close_preview()

    if not os.path.isfile(abs_path):
        return False

    try:
        proc = subprocess.Popen(
            ["qlmanage", "-p", abs_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _current_process = proc
        _current_path = abs_path
        _bring_to_front(proc)
        return True
    except OSError:
        _current_process = None
        _current_path = None
        return False


# ---------------------------------------------------------------------------
# Sanitization, image probing, and safe background loading
# ---------------------------------------------------------------------------


def sanitize_terminal_text(text: str) -> str:
    """Neutralize ESC and C0/C1 terminal control characters while keeping tabs/newlines."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for ch in normalized:
        code = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif code == 0x1B:
            out.append("^[")
        elif code < 0x20 or code == 0x7F or (0x80 <= code <= 0x9F):
            out.append("\ufffd")
        else:
            out.append(ch)
    return "".join(out)


def format_size_bytes(n: int | None) -> str:
    if n is None:
        return "unknown"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{n} B"
            return f"{size:.1f} {unit} ({n:,} B)"
        size /= 1024
    return f"{n:,} B"


def format_timestamp(ts: float | None) -> str:
    if ts is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "unknown"


def is_image_engine_available() -> bool:
    """Return True if optional Pillow and textual-image packages can be imported."""
    if os.environ.get("COMMANDERTUI_DISABLE_IMAGES") == "1":
        return False
    return (
        importlib.util.find_spec("PIL") is not None and importlib.util.find_spec("textual_image") is not None
    )


def prepare_image_terminal() -> None:
    """Probe terminal image capabilities before Textual takes over stdin.

    Called once from CLI startup when running interactively in a real TTY.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("COMMANDERTUI_NO_IMAGE_PROBE") == "1":
        return
    if not (sys.__stdout__ and sys.__stdin__):
        return
    try:
        if not (sys.__stdout__.isatty() and sys.__stdin__.isatty()):
            return
    except Exception:
        return
    if not is_image_engine_available():
        return
    try:
        import textual_image.widget  # noqa: F401
    except Exception:
        pass


def ensure_safe_image_terminal() -> None:
    """Prevent `textual_image` from probing stdin/stdout once Textual is running."""
    if importlib.util.find_spec("textual_image") is None:
        return
    try:
        import textual_image._terminal as ti_term

        if not hasattr(ti_term.probe_terminal, "_result"):
            setattr(
                ti_term.probe_terminal,
                "_result",
                ti_term.TerminalCapabilities(ti_term.CellSize(10, 20), sixel=False, tgp=False),
            )
    except Exception:
        pass


def parse_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Extract (width, height) from common image headers without Pillow."""
    try:
        if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if w > 0 and h > 0 else None

        if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return (w, h) if w > 0 and h > 0 else None

        if len(data) >= 26 and data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            h = abs(h)
            return (w, h) if w > 0 and h > 0 else None

        if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X" and len(data) >= 30:
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return (w, h)
            if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
                w, h = struct.unpack("<HH", data[26:30])
                return (w & 0x3FFF, h & 0x3FFF)
            if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
                b0, b1, b2, b3 = data[21:25]
                bits = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
                w = (bits & 0x3FFF) + 1
                h = ((bits >> 14) & 0x3FFF) + 1
                return (w, h)

        if len(data) >= 4 and data[:2] == b"\xff\xd8":
            i = 2
            n = len(data)
            while i + 3 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                while i < n and data[i] == 0xFF:
                    i += 1
                if i >= n:
                    break
                marker = data[i]
                i += 1
                if marker in (0xD8, 0xD9, 0x00) or 0xD0 <= marker <= 0xD7:
                    continue
                if i + 2 > n:
                    break
                seg_len = struct.unpack(">H", data[i : i + 2])[0]
                if seg_len < 2:
                    break
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    if i + 7 <= n:
                        h, w = struct.unpack(">HH", data[i + 3 : i + 7])
                        return (w, h) if w > 0 and h > 0 else None
                    break
                i += seg_len
    except (IndexError, ValueError, struct.error):
        return None
    return None


def _classify_by_extension(path: str) -> tuple[str, str | None]:
    """Return (category, human_type_label) from filename/extension."""
    name = os.path.basename(path)
    lower_name = name.lower()
    _, ext = os.path.splitext(lower_name)
    if ext in MARKDOWN_EXTS:
        return "markdown", "Markdown document"
    if ext in IMAGE_EXTS:
        return "image", f"Image ({ext.lstrip('.').upper()})"
    if ext in PDF_EXTS:
        return "pdf", "PDF document"
    if ext in DOCX_EXTS:
        return "docx", "Word document (DOCX)"
    if ext in XLSX_EXTS:
        return "xlsx", "Excel workbook (XLSX)"
    if ext in CSV_EXTS:
        return ("tsv", "TSV spreadsheet") if ext == ".tsv" else ("csv", "CSV spreadsheet")
    if ext in LEGACY_OFFICE_EXTS:
        if ext in (".xls",):
            return "legacy_office", "Legacy Excel workbook (.xls)"
        if ext in (".doc",):
            return "legacy_office", "Legacy Word document (.doc)"
        if ext in (".ods", ".odt", ".odp"):
            return "legacy_office", f"OpenDocument ({ext.lstrip('.').upper()})"
        if ext in (".xlsm", ".docm", ".pptm"):
            return "legacy_office", f"Macro-enabled Office ({ext.lstrip('.').upper()})"
        return "legacy_office", f"Office document ({ext.lstrip('.').upper()})"
    if ext in VIDEO_EXTS:
        return "video", f"Video ({ext.lstrip('.').upper()})"
    if ext in AUDIO_EXTS:
        return "audio", f"Audio ({ext.lstrip('.').upper()})"
    if ext in ARCHIVE_EXTS or lower_name.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")):
        return "archive", "Archive"
    if ext in TEXT_EXTS or lower_name in TEXT_FILENAMES:
        return "text", "Text file"
    return "unknown", "File"


def _special_file_label(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "Directory"
    if stat.S_ISFIFO(mode):
        return "Named pipe (FIFO)"
    if stat.S_ISSOCK(mode):
        return "Socket"
    if stat.S_ISCHR(mode):
        return "Character device"
    if stat.S_ISBLK(mode):
        return "Block device"
    return "Special file"


def collect_metadata(path: str) -> tuple[FileMetadata, os.stat_result | None, bool, str | None]:
    """Inspect path with lstat/stat without reading file contents.

    Returns (metadata, target_stat, is_readable_regular_file, error_note).
    """
    abs_path = os.path.abspath(path)
    raw_name = os.path.basename(abs_path) or abs_path
    name = sanitize_terminal_text(raw_name)
    safe_path = sanitize_terminal_text(abs_path)
    mime_guess, _ = mimetypes.guess_type(abs_path)
    mime_type = sanitize_terminal_text(mime_guess or "application/octet-stream")
    _, human_type = _classify_by_extension(abs_path)

    try:
        lst = os.lstat(abs_path)
    except OSError as exc:
        meta = FileMetadata(
            name=name,
            path=safe_path,
            size_bytes=None,
            mtime=None,
            ctime=None,
            mode_str="?",
            octal_mode="?",
            is_symlink=False,
            symlink_target=None,
            file_type="Unavailable",
            mime_type=mime_type,
        )
        return meta, None, False, sanitize_terminal_text(str(exc))

    is_symlink = stat.S_ISLNK(lst.st_mode)
    symlink_target: str | None = None
    target_stat: os.stat_result | None = lst
    note: str | None = None

    if is_symlink:
        try:
            symlink_target = sanitize_terminal_text(os.readlink(abs_path))
        except OSError:
            symlink_target = "(unreadable link)"
        try:
            target_stat = os.stat(abs_path)
        except OSError as exc:
            target_stat = None
            note = f"Broken symlink ({sanitize_terminal_text(str(exc))})."

    st_for_display = target_stat or lst
    mode_str = stat.filemode(st_for_display.st_mode)
    octal_mode = oct(stat.S_IMODE(st_for_display.st_mode))[2:].zfill(3)

    if target_stat is None:
        meta = FileMetadata(
            name=name,
            path=safe_path,
            size_bytes=None,
            mtime=lst.st_mtime,
            ctime=lst.st_ctime,
            mode_str=stat.filemode(lst.st_mode),
            octal_mode=oct(stat.S_IMODE(lst.st_mode))[2:].zfill(3),
            is_symlink=True,
            symlink_target=symlink_target,
            file_type="Broken symlink",
            mime_type=mime_type,
        )
        return meta, None, False, note

    if not stat.S_ISREG(target_stat.st_mode):
        special_type = _special_file_label(target_stat.st_mode)
        meta = FileMetadata(
            name=name,
            path=safe_path,
            size_bytes=None if stat.S_ISDIR(target_stat.st_mode) else target_stat.st_size,
            mtime=target_stat.st_mtime,
            ctime=target_stat.st_ctime,
            mode_str=mode_str,
            octal_mode=octal_mode,
            is_symlink=is_symlink,
            symlink_target=symlink_target,
            file_type=special_type,
            mime_type=mime_type,
        )
        return meta, target_stat, False, f"{special_type}: content is not read."

    meta = FileMetadata(
        name=name,
        path=safe_path,
        size_bytes=target_stat.st_size,
        mtime=target_stat.st_mtime,
        ctime=target_stat.st_ctime,
        mode_str=mode_str,
        octal_mode=octal_mode,
        is_symlink=is_symlink,
        symlink_target=symlink_target,
        file_type=human_type,
        mime_type=mime_type,
    )
    return meta, target_stat, True, None


def _read_stream_bounded(
    fh,
    byte_limit: int,
    deadline: float,
    timeout: float,
    cancel: Callable[[], bool] | None,
    *,
    line_limit: int | None = None,
    initial: bytes = b"",
) -> tuple[bytes, str | None]:
    """Read from an already-open binary stream up to `byte_limit` / `line_limit` / `deadline`."""
    chunks: list[bytes] = []
    total = 0
    lines = 0
    truncated: str | None = None

    def _consume(chunk: bytes) -> bool:
        nonlocal total, lines, truncated
        if total + len(chunk) > byte_limit:
            keep = byte_limit - total
            if keep > 0:
                chunks.append(chunk[:keep])
                total += keep
            truncated = f"Truncated at {format_size_bytes(byte_limit)}"
            return False

        if line_limit is not None:
            chunk_lines = chunk.count(b"\n")
            if lines + chunk_lines >= line_limit:
                needed = line_limit - lines
                pos = -1
                for _ in range(needed):
                    pos = chunk.find(b"\n", pos + 1)
                    if pos == -1:
                        break
                if pos != -1:
                    chunks.append(chunk[: pos + 1])
                    total += pos + 1
                else:
                    chunks.append(chunk)
                    total += len(chunk)
                truncated = f"Truncated at {line_limit:,} lines"
                return False
            lines += chunk_lines

        chunks.append(chunk)
        total += len(chunk)
        return True

    if initial:
        if not _consume(initial):
            return b"".join(chunks), truncated

    while True:
        if cancel and cancel():
            raise PreviewCancelledError()
        if deadline - time.monotonic() <= 0:
            truncated = f"Read timed out after {timeout:.1f}s"
            break
        to_read = min(READ_CHUNK_SIZE, byte_limit - total + 1)
        if to_read <= 0:
            truncated = f"Truncated at {format_size_bytes(byte_limit)}"
            break
        chunk = fh.read(to_read)
        if not chunk:
            break
        if cancel and cancel():
            raise PreviewCancelledError()
        if not _consume(chunk):
            break

    return b"".join(chunks), truncated


def _read_bounded(
    path: str,
    byte_limit: int,
    timeout: float,
    cancel: Callable[[], bool] | None,
    *,
    line_limit: int | None = None,
) -> tuple[bytes, str | None]:
    """Read up to `byte_limit` (and optional `line_limit`) within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    with open(path, "rb") as fh:
        return _read_stream_bounded(fh, byte_limit, deadline, timeout, cancel, line_limit=line_limit)


def _looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    # UTF-16 BOMs legitimately contain NUL bytes
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    if b"\x00" in sample:
        return True
    non_text = sum(1 for b in sample if b < 0x09 or (0x0E <= b <= 0x1F and b != 0x1B) or b == 0x7F)
    return (non_text / len(sample)) > 0.30


def _decode_text(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace"), "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace"), "utf-16-be"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252 (lossy)"


def _sniff_magic_category(sample: bytes) -> tuple[str, str] | None:
    if sample.startswith(b"%PDF-"):
        return "pdf", "PDF document"
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "Image (PNG)"
    if sample.startswith(b"\xff\xd8\xff"):
        return "image", "Image (JPEG)"
    if sample.startswith((b"GIF87a", b"GIF89a")):
        return "image", "Image (GIF)"
    if len(sample) >= 12 and sample[:4] == b"RIFF" and sample[8:12] == b"WEBP":
        return "image", "Image (WEBP)"
    if sample.startswith(b"BM") and len(sample) >= 26:
        return "image", "Image (BMP)"
    if sample.startswith(b"\x7fELF"):
        return "binary", "ELF binary"
    if sample.startswith(b"SQLite format 3\x00"):
        return "binary", "SQLite database"
    if sample.startswith(b"PK\x03\x04"):
        return "archive", "ZIP / packaged file"
    return None


def _load_image_preview(
    abs_path: str,
    meta: FileMetadata,
    st: os.stat_result,
    cancel: Callable[[], bool] | None,
    byte_limit: int,
    pixel_limit: int,
    max_dim: int,
    timeout: float,
) -> PreviewResult:
    engine_ok = is_image_engine_available()
    deadline = time.monotonic() + timeout

    def _abort_if_needed() -> PreviewResult | None:
        if cancel and cancel():
            return PreviewResult(
                kind=PreviewKind.ERROR,
                path=abs_path,
                metadata=meta,
                image_engine_available=engine_ok,
                cancelled=True,
            )
        if deadline - time.monotonic() <= 0:
            return PreviewResult(
                kind=PreviewKind.ERROR,
                path=abs_path,
                metadata=meta,
                image_engine_available=engine_ok,
                error=f"Image preview timed out after {timeout:.1f}s.",
            )
        return None

    if st.st_size == 0:
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            image_engine_available=engine_ok,
            note="Empty image file (0 bytes).",
        )

    if st.st_size > byte_limit:
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            image_engine_available=engine_ok,
            note=f"Image exceeds preview byte limit ({format_size_bytes(st.st_size)} > {format_size_bytes(byte_limit)}).",
        )

    abort = _abort_if_needed()
    if abort is not None:
        return abort

    if not engine_ok:
        try:
            header, _ = _read_bounded(
                abs_path,
                min(st.st_size, 64 * 1024),
                max(0.0, deadline - time.monotonic()),
                cancel,
            )
        except PreviewCancelledError:
            return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=meta, cancelled=True)
        except OSError as exc:
            return PreviewResult(
                kind=PreviewKind.ERROR,
                path=abs_path,
                metadata=meta,
                error=sanitize_terminal_text(str(exc)),
            )
        abort = _abort_if_needed()
        if abort is not None:
            return abort
        dims = parse_image_dimensions(header)
        meta.image_dimensions = dims
        return PreviewResult(
            kind=PreviewKind.IMAGE,
            path=abs_path,
            metadata=meta,
            pil_image=None,
            image_engine_available=False,
            note='Optional image preview engine not installed (install with: pip install "commandertui[images]").',
        )

    try:
        raw, truncated = _read_bounded(
            abs_path,
            byte_limit,
            max(0.0, deadline - time.monotonic()),
            cancel,
        )
    except PreviewCancelledError:
        return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=meta, cancelled=True)
    except OSError as exc:
        return PreviewResult(
            kind=PreviewKind.ERROR,
            path=abs_path,
            metadata=meta,
            image_engine_available=True,
            error=sanitize_terminal_text(str(exc)),
        )

    abort = _abort_if_needed()
    if abort is not None:
        return abort

    if truncated is not None:
        meta.image_dimensions = parse_image_dimensions(raw[: 64 * 1024])
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            image_engine_available=True,
            note=f"Image preview skipped: {truncated}.",
        )

    header_dims = parse_image_dimensions(raw[: 64 * 1024])
    if header_dims is not None:
        meta.image_dimensions = header_dims
        hw, hh = header_dims
        if hw <= 0 or hh <= 0:
            return PreviewResult(
                kind=PreviewKind.METADATA,
                path=abs_path,
                metadata=meta,
                image_engine_available=True,
                note="Invalid image dimensions.",
            )
        if hw * hh > pixel_limit:
            return PreviewResult(
                kind=PreviewKind.METADATA,
                path=abs_path,
                metadata=meta,
                image_engine_available=True,
                note=f"Image exceeds pixel limit ({hw}x{hh} = {hw * hh:,} px > {pixel_limit:,} px).",
            )

    try:
        from PIL import Image as PILImage
        from PIL import ImageOps, UnidentifiedImageError
    except ImportError:
        meta.image_dimensions = header_dims
        return PreviewResult(
            kind=PreviewKind.IMAGE,
            path=abs_path,
            metadata=meta,
            pil_image=None,
            image_engine_available=False,
            note='Optional image preview engine not installed (install with: pip install "commandertui[images]").',
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", PILImage.DecompressionBombWarning)
            with PILImage.open(io.BytesIO(raw)) as opened:
                abort = _abort_if_needed()
                if abort is not None:
                    return abort
                width, height = opened.size
                meta.image_dimensions = (width, height)
                if width <= 0 or height <= 0:
                    return PreviewResult(
                        kind=PreviewKind.METADATA,
                        path=abs_path,
                        metadata=meta,
                        image_engine_available=True,
                        note="Invalid image dimensions.",
                    )
                if width * height > pixel_limit:
                    return PreviewResult(
                        kind=PreviewKind.METADATA,
                        path=abs_path,
                        metadata=meta,
                        image_engine_available=True,
                        note=f"Image exceeds pixel limit ({width}x{height} = {width * height:,} px > {pixel_limit:,} px).",
                    )

                transposed = ImageOps.exif_transpose(opened)
                abort = _abort_if_needed()
                if abort is not None:
                    return abort
                if transposed.size != meta.image_dimensions:
                    meta.image_dimensions = transposed.size
                if transposed.width > max_dim or transposed.height > max_dim:
                    transposed.thumbnail((max_dim, max_dim))
                    abort = _abort_if_needed()
                    if abort is not None:
                        return abort
                if transposed.mode not in ("RGB", "RGBA"):
                    converted = transposed.convert("RGBA" if "A" in transposed.getbands() else "RGB")
                else:
                    converted = transposed.copy()
                abort = _abort_if_needed()
                if abort is not None:
                    return abort
                converted.load()
                abort = _abort_if_needed()
                if abort is not None:
                    return abort
                return PreviewResult(
                    kind=PreviewKind.IMAGE,
                    path=abs_path,
                    metadata=meta,
                    pil_image=converted,
                    image_engine_available=True,
                )
    except PILImage.DecompressionBombError as exc:
        meta.image_dimensions = meta.image_dimensions or parse_image_dimensions(raw[: 64 * 1024])
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            image_engine_available=True,
            note=f"Image exceeds decoder pixel limit: {sanitize_terminal_text(str(exc))}",
        )
    except (UnidentifiedImageError, OSError, ValueError, EOFError) as exc:
        meta.image_dimensions = meta.image_dimensions or parse_image_dimensions(raw[: 64 * 1024])
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            image_engine_available=True,
            note=f"Could not decode image: {sanitize_terminal_text(str(exc))}",
        )


def load_preview(
    path: str,
    cancel: Callable[[], bool] | None = None,
    *,
    text_byte_limit: int = MAX_TEXT_BYTES,
    text_line_limit: int = MAX_TEXT_LINES,
    text_timeout: float = TEXT_READ_TIMEOUT,
    image_byte_limit: int = MAX_IMAGE_BYTES,
    image_pixel_limit: int = MAX_IMAGE_PIXELS,
    image_max_dim: int = MAX_IMAGE_DIM,
    image_timeout: float = IMAGE_READ_TIMEOUT,
    page_index: int = 0,
    pdf_visual: bool = False,
    sheet_index: int = 0,
) -> PreviewResult:
    """Load file preview safely on demand (intended for background worker threads)."""
    abs_path = os.path.abspath(path)
    meta, target_stat, is_reg, meta_note = collect_metadata(abs_path)

    if cancel and cancel():
        return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=meta, cancelled=True)

    if not is_reg or target_stat is None:
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=meta_note or "Unsupported item type.",
        )

    category, human_type = _classify_by_extension(abs_path)
    meta.file_type = human_type

    if category == "legacy_office":
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=(
                f"Integrated preview is not enabled for {human_type} "
                "(only modern .docx, .xlsx, .csv/.tsv, and .pdf are supported; no external converters used)."
            ),
        )

    if category in ("video", "audio", "archive"):
        return PreviewResult(
            kind=PreviewKind.METADATA,
            path=abs_path,
            metadata=meta,
            note=f"Integrated preview is not enabled for {human_type}.",
        )

    if category == "image":
        return _load_image_preview(
            abs_path,
            meta,
            target_stat,
            cancel,
            image_byte_limit,
            image_pixel_limit,
            image_max_dim,
            image_timeout,
        )

    if category in ("pdf", "docx", "xlsx", "csv", "tsv"):
        from . import preview_docs

        if category == "pdf":
            return preview_docs.load_pdf_preview(
                abs_path,
                meta,
                target_stat,
                cancel,
                page_index=page_index,
                render_visual=pdf_visual,
                pixel_limit=image_pixel_limit,
                max_dim=image_max_dim,
            )
        if category == "docx":
            return preview_docs.load_docx_preview(abs_path, meta, target_stat, cancel)
        if category == "xlsx":
            return preview_docs.load_xlsx_preview(
                abs_path,
                meta,
                target_stat,
                cancel,
                sheet_index=sheet_index,
            )
        return preview_docs.load_csv_preview(
            abs_path,
            meta,
            target_stat,
            cancel,
            delimiter="\t" if category == "tsv" else ",",
        )

    # For text, markdown, or unknown files: open once, sniff first chunk, then continue streaming
    if target_stat.st_size == 0:
        kind = PreviewKind.MARKDOWN if category == "markdown" else PreviewKind.TEXT
        return PreviewResult(
            kind=kind,
            path=abs_path,
            metadata=meta,
            text="",
            encoding="utf-8",
            note="Empty file (0 bytes).",
        )

    deadline = time.monotonic() + text_timeout
    try:
        with open(abs_path, "rb") as fh:
            if cancel and cancel():
                return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=meta, cancelled=True)
            if deadline - time.monotonic() <= 0:
                return PreviewResult(
                    kind=PreviewKind.ERROR,
                    path=abs_path,
                    metadata=meta,
                    error=f"Read timed out after {text_timeout:.1f}s",
                )
            sample = fh.read(min(SNIFF_BYTES, max(1, text_byte_limit)))
            if cancel and cancel():
                return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=meta, cancelled=True)
            if not sample and deadline - time.monotonic() <= 0:
                return PreviewResult(
                    kind=PreviewKind.ERROR,
                    path=abs_path,
                    metadata=meta,
                    error=f"Read timed out after {text_timeout:.1f}s",
                )

            if category == "unknown":
                magic = _sniff_magic_category(sample)
                if magic is not None:
                    magic_cat, magic_label = magic
                    meta.file_type = magic_label
                    if magic_cat == "image":
                        fh.close()
                        return _load_image_preview(
                            abs_path,
                            meta,
                            target_stat,
                            cancel,
                            image_byte_limit,
                            image_pixel_limit,
                            image_max_dim,
                            image_timeout,
                        )
                    if magic_cat == "pdf":
                        from . import preview_docs

                        fh.close()
                        return preview_docs.load_pdf_preview(
                            abs_path,
                            meta,
                            target_stat,
                            cancel,
                            page_index=page_index,
                            render_visual=pdf_visual,
                            pixel_limit=image_pixel_limit,
                            max_dim=image_max_dim,
                        )
                    return PreviewResult(
                        kind=PreviewKind.METADATA,
                        path=abs_path,
                        metadata=meta,
                        note=f"Integrated preview is not enabled for {magic_label}.",
                    )

            if _looks_binary(sample):
                if category == "unknown":
                    meta.file_type = "Binary file"
                return PreviewResult(
                    kind=PreviewKind.METADATA,
                    path=abs_path,
                    metadata=meta,
                    note="Binary content detected; showing metadata only.",
                )

            raw, truncated = _read_stream_bounded(
                fh,
                text_byte_limit,
                deadline,
                text_timeout,
                cancel,
                line_limit=text_line_limit,
                initial=sample,
            )
    except PreviewCancelledError:
        return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=meta, cancelled=True)
    except OSError as exc:
        return PreviewResult(
            kind=PreviewKind.ERROR,
            path=abs_path,
            metadata=meta,
            error=sanitize_terminal_text(str(exc)),
        )

    if not raw and truncated is not None:
        return PreviewResult(
            kind=PreviewKind.ERROR,
            path=abs_path,
            metadata=meta,
            error=truncated,
        )

    decoded, encoding = _decode_text(raw)
    sanitized = sanitize_terminal_text(decoded)
    kind = PreviewKind.MARKDOWN if category == "markdown" else PreviewKind.TEXT

    if truncated is None and target_stat.st_size > len(raw):
        truncated = f"Truncated at {format_size_bytes(text_byte_limit)}"

    return PreviewResult(
        kind=kind,
        path=abs_path,
        metadata=meta,
        text=sanitized,
        truncated_reason=truncated,
        encoding=encoding,
    )


def _minimal_fallback_metadata(abs_path: str) -> FileMetadata:
    name = sanitize_terminal_text(os.path.basename(abs_path) or abs_path)
    return FileMetadata(
        name=name,
        path=sanitize_terminal_text(abs_path),
        size_bytes=None,
        mtime=None,
        ctime=None,
        mode_str="unknown",
        octal_mode="---",
        is_symlink=False,
        symlink_target=None,
        file_type="File",
        mime_type=None,
    )


def _terminate_process_group(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=0.15)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _result_to_json_dict(result: PreviewResult, *, has_image: bool) -> dict[str, Any]:
    return {
        "kind": result.kind.value,
        "path": result.path,
        "metadata": asdict(result.metadata),
        "text": result.text,
        "has_image": has_image,
        "truncated_reason": result.truncated_reason,
        "encoding": result.encoding,
        "note": result.note,
        "error": result.error,
        "cancelled": result.cancelled,
        "image_engine_available": result.image_engine_available,
        "page_index": result.page_index,
        "page_count": result.page_count,
        "pdf_visual_available": result.pdf_visual_available,
        "pdf_visual_mode": result.pdf_visual_mode,
        "table_headers": result.table_headers,
        "table_rows": result.table_rows,
        "sheet_names": result.sheet_names,
        "sheet_index": result.sheet_index,
    }


def _result_from_json_dict(payload: dict[str, Any], pil_image: Any = None) -> PreviewResult:
    raw_meta = dict(payload["metadata"])
    dims = raw_meta.get("image_dimensions")
    if isinstance(dims, list) and len(dims) == 2:
        raw_meta["image_dimensions"] = (int(dims[0]), int(dims[1]))
    elif dims is not None:
        raw_meta["image_dimensions"] = tuple(dims)
    meta = FileMetadata(**raw_meta)
    return PreviewResult(
        kind=PreviewKind(payload["kind"]),
        path=payload["path"],
        metadata=meta,
        text=payload.get("text"),
        pil_image=pil_image,
        truncated_reason=payload.get("truncated_reason"),
        encoding=payload.get("encoding"),
        note=payload.get("note"),
        error=payload.get("error"),
        cancelled=bool(payload.get("cancelled", False)),
        image_engine_available=bool(payload.get("image_engine_available", False)),
        page_index=int(payload.get("page_index", 0)),
        page_count=int(payload.get("page_count", 0)),
        pdf_visual_available=bool(payload.get("pdf_visual_available", False)),
        pdf_visual_mode=bool(payload.get("pdf_visual_mode", False)),
        table_headers=payload.get("table_headers"),
        table_rows=payload.get("table_rows"),
        sheet_names=payload.get("sheet_names"),
        sheet_index=int(payload.get("sheet_index", 0)),
    )


def load_preview_isolated(
    path: str,
    cancel: Callable[[], bool] | None = None,
    *,
    text_byte_limit: int = MAX_TEXT_BYTES,
    text_line_limit: int = MAX_TEXT_LINES,
    text_timeout: float = TEXT_READ_TIMEOUT,
    image_byte_limit: int = MAX_IMAGE_BYTES,
    image_pixel_limit: int = MAX_IMAGE_PIXELS,
    image_max_dim: int = MAX_IMAGE_DIM,
    image_timeout: float = IMAGE_READ_TIMEOUT,
    page_index: int = 0,
    pdf_visual: bool = False,
    sheet_index: int = 0,
    wall_timeout: float | None = None,
) -> PreviewResult:
    """Run `load_preview` in a dedicated process group that can be hard-killed on cancel/timeout."""
    from . import preview_docs

    abs_path = os.path.abspath(path)
    fallback_meta = _minimal_fallback_metadata(abs_path)

    if cancel and cancel():
        return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=fallback_meta, cancelled=True)

    if wall_timeout is None:
        category, _ = _classify_by_extension(abs_path)
        if category == "image":
            base_timeout = image_timeout
        elif category in ("pdf", "docx", "xlsx", "csv", "tsv"):
            base_timeout = preview_docs.DOC_PREVIEW_TIMEOUT
        else:
            base_timeout = text_timeout
        effective_wall_timeout = max(0.2, base_timeout + ISOLATED_PREVIEW_GRACE_SECONDS)
    else:
        effective_wall_timeout = max(0.01, wall_timeout)

    env = os.environ.copy()
    if not is_image_engine_available():
        env["COMMANDERTUI_DISABLE_IMAGES"] = "1"
    if (
        not preview_docs.is_pdf_text_engine_available()
        and not preview_docs.is_docx_engine_available()
        and not preview_docs.is_xlsx_engine_available()
    ):
        env["COMMANDERTUI_DISABLE_DOCUMENTS"] = "1"
    if not preview_docs.is_pdf_render_engine_available():
        env["COMMANDERTUI_DISABLE_PDF_RENDER"] = "1"

    with tempfile.TemporaryDirectory(prefix="commandertui-preview-") as tmpdir:
        result_json = os.path.join(tmpdir, "result.json")
        image_png = os.path.join(tmpdir, "image.png")
        cmd = [
            sys.executable,
            "-m",
            "commandertui.preview",
            "--worker",
            abs_path,
            "--result-out",
            result_json,
            "--image-out",
            image_png,
            "--text-byte-limit",
            str(text_byte_limit),
            "--text-line-limit",
            str(text_line_limit),
            "--text-timeout",
            str(text_timeout),
            "--image-byte-limit",
            str(image_byte_limit),
            "--image-pixel-limit",
            str(image_pixel_limit),
            "--image-max-dim",
            str(image_max_dim),
            "--image-timeout",
            str(image_timeout),
            "--page-index",
            str(page_index),
            "--sheet-index",
            str(sheet_index),
        ]
        if pdf_visual:
            cmd.append("--pdf-visual")
        stderr_path = os.path.join(tmpdir, "stderr.log")
        try:
            stderr_fh = open(stderr_path, "wb")
        except OSError as exc:
            return PreviewResult(
                kind=PreviewKind.ERROR,
                path=abs_path,
                metadata=fallback_meta,
                error=f"Failed to prepare preview worker: {sanitize_terminal_text(str(exc))}",
            )
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_fh,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            stderr_fh.close()
            return PreviewResult(
                kind=PreviewKind.ERROR,
                path=abs_path,
                metadata=fallback_meta,
                error=f"Failed to start preview worker: {sanitize_terminal_text(str(exc))}",
            )
        stderr_fh.close()

        deadline = time.monotonic() + effective_wall_timeout
        timed_out = False
        try:
            while True:
                if cancel and cancel():
                    _terminate_process_group(proc)
                    return PreviewResult(
                        kind=PreviewKind.ERROR,
                        path=abs_path,
                        metadata=fallback_meta,
                        cancelled=True,
                    )
                rc = proc.poll()
                if rc is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _terminate_process_group(proc)
                    break
                time.sleep(min(0.02, remaining))
        finally:
            if proc.poll() is None:
                _terminate_process_group(proc)

        if timed_out:
            return PreviewResult(
                kind=PreviewKind.ERROR,
                path=abs_path,
                metadata=fallback_meta,
                error=f"Preview worker timed out after {effective_wall_timeout:.1f}s.",
            )

        if cancel and cancel():
            return PreviewResult(
                kind=PreviewKind.ERROR, path=abs_path, metadata=fallback_meta, cancelled=True
            )

        if not os.path.isfile(result_json):
            stderr_bytes = b""
            try:
                with open(stderr_path, "rb") as err_fh:
                    err_fh.seek(0, os.SEEK_END)
                    size = err_fh.tell()
                    err_fh.seek(max(0, size - 8192), os.SEEK_SET)
                    stderr_bytes = err_fh.read(8192)
            except OSError:
                stderr_bytes = b""
            err_detail = sanitize_terminal_text(stderr_bytes.decode("utf-8", errors="replace").strip())
            rc = proc.returncode
            if rc is not None and rc < 0:
                msg = f"Preview worker terminated by signal {-rc}"
            else:
                msg = f"Preview worker failed (exit {rc})"
            if err_detail:
                msg = f"{msg}: {err_detail.splitlines()[-1]}"
            return PreviewResult(kind=PreviewKind.ERROR, path=abs_path, metadata=fallback_meta, error=msg)

        try:
            with open(result_json, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            pil_image = None
            if payload.get("has_image") and os.path.isfile(image_png):
                from PIL import Image as PILImage

                with PILImage.open(image_png) as decoded:
                    decoded.load()
                    pil_image = decoded.copy()
            return _result_from_json_dict(payload, pil_image=pil_image)
        except Exception as exc:
            return PreviewResult(
                kind=PreviewKind.ERROR,
                path=abs_path,
                metadata=fallback_meta,
                error=f"Invalid preview worker output: {sanitize_terminal_text(str(exc))}",
            )


def _worker_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--result-out", required=True)
    parser.add_argument("--image-out", required=True)
    parser.add_argument("--text-byte-limit", type=int, default=MAX_TEXT_BYTES)
    parser.add_argument("--text-line-limit", type=int, default=MAX_TEXT_LINES)
    parser.add_argument("--text-timeout", type=float, default=TEXT_READ_TIMEOUT)
    parser.add_argument("--image-byte-limit", type=int, default=MAX_IMAGE_BYTES)
    parser.add_argument("--image-pixel-limit", type=int, default=MAX_IMAGE_PIXELS)
    parser.add_argument("--image-max-dim", type=int, default=MAX_IMAGE_DIM)
    parser.add_argument("--image-timeout", type=float, default=IMAGE_READ_TIMEOUT)
    parser.add_argument("--page-index", type=int, default=0)
    parser.add_argument("--sheet-index", type=int, default=0)
    parser.add_argument("--pdf-visual", action="store_true")
    ns = parser.parse_args(argv)

    ensure_safe_image_terminal()
    try:
        res = load_preview(
            ns.worker,
            text_byte_limit=ns.text_byte_limit,
            text_line_limit=ns.text_line_limit,
            text_timeout=ns.text_timeout,
            image_byte_limit=ns.image_byte_limit,
            image_pixel_limit=ns.image_pixel_limit,
            image_max_dim=ns.image_max_dim,
            image_timeout=ns.image_timeout,
            page_index=ns.page_index,
            pdf_visual=ns.pdf_visual,
            sheet_index=ns.sheet_index,
        )
    except Exception as exc:
        res = PreviewResult(
            kind=PreviewKind.ERROR,
            path=os.path.abspath(ns.worker),
            metadata=_minimal_fallback_metadata(os.path.abspath(ns.worker)),
            error=f"Unhandled preview worker error: {sanitize_terminal_text(str(exc))}",
        )

    has_image = False
    if res.pil_image is not None:
        try:
            res.pil_image.save(ns.image_out, format="PNG")
            has_image = True
        except Exception as exc:
            res = PreviewResult(
                kind=PreviewKind.METADATA,
                path=res.path,
                metadata=res.metadata,
                note=f"Could not serialize preview image: {sanitize_terminal_text(str(exc))}",
            )

    tmp_result = f"{ns.result_out}.tmp"
    try:
        with open(tmp_result, "w", encoding="utf-8") as fh:
            json.dump(_result_to_json_dict(res, has_image=has_image), fh)
        os.replace(tmp_result, ns.result_out)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
