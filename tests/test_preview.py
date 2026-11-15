import os
import struct
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Markdown, Static, TextArea

from commandertui import preview, settings
from commandertui.modals import MessageScreen
from commandertui.preview import FileMetadata, PreviewKind, PreviewResult
from commandertui.preview_screen import PreviewScreen
from commandertui.widgets import FunctionBar


@pytest.fixture(autouse=True)
def _cleanup_preview():
    yield
    preview.close_preview()


def test_is_macos():
    with patch("sys.platform", "darwin"):
        assert preview.is_macos() is True
    with patch("sys.platform", "linux"):
        assert preview.is_macos() is False


def test_function_bar_includes_preview_on_linux_and_darwin():
    for plat in ("darwin", "linux"):
        with patch("sys.platform", plat):
            keys = FunctionBar.get_keys()
            assert any(k[0] == "v" and k[1] == "Preview" and k[2] == "preview_file" for k in keys)


def test_toggle_preview_non_macos(tmp_path):
    test_file = tmp_path / "doc.txt"
    test_file.write_text("hello")

    with patch("sys.platform", "linux"):
        assert preview.toggle_preview(str(test_file)) is False
        assert preview.is_active() is False
        assert preview.current_path() is None


def test_toggle_preview_directory_fails(tmp_path):
    with patch("sys.platform", "darwin"):
        assert preview.toggle_preview(str(tmp_path)) is False
        assert preview.is_active() is False


def test_toggle_preview_lifecycle(tmp_path):
    file1 = tmp_path / "one.txt"
    file1.write_text("content 1")
    file2 = tmp_path / "two.txt"
    file2.write_text("content 2")

    mock_proc1 = MagicMock(spec=subprocess.Popen)
    mock_proc1.poll.return_value = None

    mock_proc2 = MagicMock(spec=subprocess.Popen)
    mock_proc2.poll.return_value = None

    with (
        patch("sys.platform", "darwin"),
        patch.object(preview, "_bring_to_front"),
        patch("subprocess.Popen", side_effect=[mock_proc1, mock_proc2]) as mock_popen,
    ):
        started = preview.toggle_preview(str(file1))
        assert started is True
        assert preview.is_active() is True
        assert preview.current_path() == str(file1)
        mock_popen.assert_called_once_with(
            ["qlmanage", "-p", str(file1)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        started = preview.toggle_preview(str(file1))
        assert started is False
        assert preview.is_active() is False
        assert preview.current_path() is None
        mock_proc1.terminate.assert_called_once()


def test_toggle_preview_reopens_after_external_close(tmp_path):
    file1 = tmp_path / "one.txt"
    file1.write_text("content 1")

    mock_proc1 = MagicMock(spec=subprocess.Popen)
    mock_proc1.poll.return_value = None

    mock_proc2 = MagicMock(spec=subprocess.Popen)
    mock_proc2.poll.return_value = None

    with (
        patch("sys.platform", "darwin"),
        patch.object(preview, "_bring_to_front"),
        patch("subprocess.Popen", side_effect=[mock_proc1, mock_proc2]),
    ):
        assert preview.toggle_preview(str(file1)) is True
        mock_proc1.poll.return_value = 0
        assert preview.is_active() is False
        assert preview.toggle_preview(str(file1)) is True
        assert preview.is_active() is True


def test_toggle_preview_switches_file(tmp_path):
    file1 = tmp_path / "one.txt"
    file1.write_text("content 1")
    file2 = tmp_path / "two.txt"
    file2.write_text("content 2")

    mock_proc1 = MagicMock(spec=subprocess.Popen)
    mock_proc1.poll.return_value = None

    mock_proc2 = MagicMock(spec=subprocess.Popen)
    mock_proc2.poll.return_value = None

    with (
        patch("sys.platform", "darwin"),
        patch.object(preview, "_bring_to_front"),
        patch("subprocess.Popen", side_effect=[mock_proc1, mock_proc2]),
    ):
        assert preview.toggle_preview(str(file1)) is True
        assert preview.current_path() == str(file1)

        assert preview.toggle_preview(str(file2)) is True
        mock_proc1.terminate.assert_called_once()
        assert preview.current_path() == str(file2)


def test_close_preview_handles_exceptions():
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None
    mock_proc.terminate.side_effect = OSError("boom")

    preview._current_process = mock_proc
    preview._current_path = "/fake/path"

    preview.close_preview()
    assert preview._current_process is None
    assert preview._current_path is None


def test_bring_to_front_success(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("content")
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None

    with (
        patch("sys.platform", "darwin"),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        started = preview.toggle_preview(str(f))
        assert started is True
        preview._bring_to_front_worker(mock_proc)
        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "osascript"
        assert "frontmost" in cmd[2]
        assert "qlmanage" in cmd[2]


def test_bring_to_front_failure_silently_ignored(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("content")
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None

    with (
        patch("sys.platform", "darwin"),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 3.0)),
    ):
        preview._current_process = mock_proc
        preview._current_path = str(f)
        preview._bring_to_front_worker(mock_proc)
        assert preview.is_active() is True


def test_bring_to_front_already_closed_does_not_call_osascript():
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = 0

    with patch("subprocess.run") as mock_run:
        preview._bring_to_front_worker(mock_proc)
        mock_run.assert_not_called()


def test_bring_to_front_cancelled_if_different_process():
    old_proc = MagicMock(spec=subprocess.Popen)
    old_proc.poll.return_value = None
    new_proc = MagicMock(spec=subprocess.Popen)
    new_proc.poll.return_value = None

    preview._current_process = new_proc

    with patch("subprocess.run") as mock_run:
        preview._bring_to_front_worker(old_proc)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Stage 1 loader, sanitization, limits, metadata, and image fallback tests
# ---------------------------------------------------------------------------


def test_sanitize_terminal_text_neutralizes_controls():
    raw = "ok\x1b[31mred\x1b]0;title\x07\x08\x7f\x9b2J\r\nnext\tcol\rline"
    cleaned = preview.sanitize_terminal_text(raw)
    assert "\x1b" not in cleaned
    assert "\x07" not in cleaned
    assert "\x08" not in cleaned
    assert "\x7f" not in cleaned
    assert "\x9b" not in cleaned
    assert "\r" not in cleaned
    assert "^[[31mred^[]0;title" in cleaned
    assert "next\tcol\nline" in cleaned


def test_load_preview_text_and_markdown(tmp_path):
    txt = tmp_path / "notes.log"
    txt.write_text("line1\x1b[2J\nline2\n", encoding="utf-8")
    res_txt = preview.load_preview(str(txt))
    assert res_txt.kind == PreviewKind.TEXT
    assert res_txt.text == "line1^[[2J\nline2\n"
    assert "\x1b" not in (res_txt.text or "")

    md = tmp_path / "README.md"
    md.write_text("# Heading\n\nBody text\n", encoding="utf-8")
    res_md = preview.load_preview(str(md))
    assert res_md.kind == PreviewKind.MARKDOWN
    assert "# Heading" in (res_md.text or "")

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    res_empty = preview.load_preview(str(empty))
    assert res_empty.kind == PreviewKind.TEXT
    assert res_empty.text == ""
    assert res_empty.note


def test_load_preview_text_byte_and_line_limits(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("A" * 500, encoding="utf-8")
    res_bytes = preview.load_preview(str(big), text_byte_limit=120)
    assert res_bytes.kind == PreviewKind.TEXT
    assert len(res_bytes.text or "") == 120
    assert res_bytes.truncated_reason and "Truncated" in res_bytes.truncated_reason

    many_lines = tmp_path / "lines.txt"
    many_lines.write_text("\n".join(f"L{i}" for i in range(50)) + "\n", encoding="utf-8")
    res_lines = preview.load_preview(str(many_lines), text_line_limit=10)
    assert res_lines.kind == PreviewKind.TEXT
    assert (res_lines.text or "").count("\n") == 10
    assert res_lines.truncated_reason and "10 lines" in res_lines.truncated_reason


def test_load_preview_timeout_and_cancellation(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text("hello world\n", encoding="utf-8")

    res_cancel = preview.load_preview(str(f), cancel=lambda: True)
    assert res_cancel.cancelled is True

    res_timeout = preview.load_preview(str(f), text_timeout=-1.0)
    assert res_timeout.kind == PreviewKind.ERROR
    assert res_timeout.error and "timed out" in res_timeout.error.lower()


def test_load_preview_unsupported_and_binary_metadata(tmp_path):
    ods = tmp_path / "sheet.ods"
    ods.write_bytes(b"PK\x03\x04dummy-ods")
    res_ods = preview.load_preview(str(ods))
    assert res_ods.kind == PreviewKind.METADATA
    assert "OpenDocument" in res_ods.metadata.file_type
    assert res_ods.note and "external converters" in res_ods.note

    xls = tmp_path / "legacy.xls"
    xls.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy")
    res_xls = preview.load_preview(str(xls))
    assert res_xls.kind == PreviewKind.METADATA
    assert "Legacy Excel" in res_xls.metadata.file_type

    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    res_mp4 = preview.load_preview(str(mp4))
    assert res_mp4.kind == PreviewKind.METADATA
    assert "Video" in res_mp4.metadata.file_type

    blob = tmp_path / "data.bin"
    blob.write_bytes(b"\x7fELF\x02\x01\x01\x00\x00\x00")
    res_blob = preview.load_preview(str(blob))
    assert res_blob.kind == PreviewKind.METADATA
    assert res_blob.metadata.file_type == "ELF binary"


def test_load_preview_special_files_and_broken_symlink(tmp_path):
    fifo = tmp_path / "my.pipe"
    os.mkfifo(fifo)
    res_fifo = preview.load_preview(str(fifo))
    assert res_fifo.kind == PreviewKind.METADATA
    assert "FIFO" in res_fifo.metadata.file_type

    broken = tmp_path / "broken_link"
    os.symlink(str(tmp_path / "missing_target"), broken)
    res_broken = preview.load_preview(str(broken))
    assert res_broken.kind == PreviewKind.METADATA
    assert res_broken.metadata.is_symlink is True
    assert res_broken.metadata.file_type == "Broken symlink"


def _minimal_png(width: int, height: int) -> bytes:
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"


@pytest.mark.skipif(
    not preview.is_image_engine_available(),
    reason="optional [images] dependencies not installed",
)
def test_load_preview_image_with_pillow_and_limits(tmp_path):
    from PIL import Image as PILImage

    img_path = tmp_path / "photo.png"
    PILImage.new("RGB", (120, 80), color=(10, 120, 240)).save(img_path, format="PNG")

    res = preview.load_preview(str(img_path), image_max_dim=60)
    assert res.kind == PreviewKind.IMAGE
    assert res.image_engine_available is True
    assert res.metadata.image_dimensions == (120, 80)
    assert res.pil_image is not None
    assert max(res.pil_image.size) <= 60

    res_px = preview.load_preview(str(img_path), image_pixel_limit=1000)
    assert res_px.kind == PreviewKind.METADATA
    assert res_px.pil_image is None
    assert "pixel limit" in (res_px.note or "").lower()

    res_bytes = preview.load_preview(str(img_path), image_byte_limit=10)
    assert res_bytes.kind == PreviewKind.METADATA
    assert res_bytes.pil_image is None
    assert "byte limit" in (res_bytes.note or "").lower()


def test_load_preview_image_without_optional_engine(tmp_path):
    img_path = tmp_path / "pic.png"
    img_path.write_bytes(_minimal_png(64, 48))

    with patch.object(preview, "is_image_engine_available", return_value=False):
        res = preview.load_preview(str(img_path))

    assert res.kind == PreviewKind.IMAGE
    assert res.image_engine_available is False
    assert res.pil_image is None
    assert res.metadata.image_dimensions == (64, 48)
    assert "commandertui[images]" in (res.note or "")


def test_parse_image_dimensions_headers():
    png = _minimal_png(33, 21)
    gif = b"GIF89a" + struct.pack("<HH", 44, 19)
    bmp = b"BM" + b"\x00" * 16 + struct.pack("<ii", 55, -29)
    jpeg = b"\xff\xd8\xff\xe0\x00\x04\x00\x00\xff\xc0\x00\x0b\x08" + struct.pack(">HH", 32, 64) + b"\x01\x00"
    webp = (
        b"RIFF"
        + b"\x00\x00\x00\x00"
        + b"WEBP"
        + b"VP8X"
        + b"\x0a\x00\x00\x00"
        + b"\x00\x00\x00\x00"
        + (79).to_bytes(3, "little")
        + (59).to_bytes(3, "little")
    )
    assert preview.parse_image_dimensions(png) == (33, 21)
    assert preview.parse_image_dimensions(gif) == (44, 19)
    assert preview.parse_image_dimensions(bmp) == (55, 29)
    assert preview.parse_image_dimensions(jpeg) == (64, 32)
    assert preview.parse_image_dimensions(webp) == (80, 60)


def test_settings_preview_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))

    assert settings.load_preview_mode() == "integrated"
    settings.save_preview_mode("quicklook")
    assert settings.load_preview_mode() == "quicklook"
    settings.save_preview_mode("integrated")
    assert settings.load_preview_mode() == "integrated"
    with pytest.raises(ValueError):
        settings.save_preview_mode("invalid")


# ---------------------------------------------------------------------------
# Pilot UI tests
# ---------------------------------------------------------------------------


async def _skip_boot(pilot):
    await pilot.pause()
    for _ in range(4):
        await pilot.press("enter")
        await pilot.pause()
        if pilot.app.screen.__class__.__name__ != "BootScreen":
            break


@pytest.mark.asyncio
async def test_main_screen_preview_action_on_folder_shows_message(tmp_path):
    (tmp_path / "subfolder").mkdir()
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        await pilot.press("v")
        await pilot.pause()
        assert isinstance(app.screen, MessageScreen)
        assert "Preview works on files only" in app.screen.text
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("down")
        await pilot.press("v")
        await pilot.pause()
        assert isinstance(app.screen, MessageScreen)
        assert "Preview works on files only" in app.screen.text


@pytest.mark.asyncio
async def test_main_screen_navigation_is_on_demand_no_auto_preview(tmp_path):
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(f"content {name}")
    other = tmp_path.parent / "other_pane"
    other.mkdir(exist_ok=True)

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    with patch.object(preview, "load_preview_isolated", wraps=preview.load_preview_isolated) as spy_load:
        async with app.run_test(size=(140, 30)) as pilot:
            await _skip_boot(pilot)
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("up")
            await pilot.pause()
            spy_load.assert_not_called()

            await pilot.press("v")
            await pilot.pause(0.3)
            assert isinstance(app.screen, PreviewScreen)
            assert spy_load.call_count == 1
            await pilot.press("escape")
            await pilot.pause()


@pytest.mark.asyncio
async def test_main_screen_preview_text_and_esc_restores_same_file(tmp_path):
    (tmp_path / "alpha.txt").write_text("alpha")
    target = tmp_path / "middle.py"
    target.write_text("print('safe')\n\x1b[31mescape_test\n")
    (tmp_path / "zeta.txt").write_text("zeta")
    other = tmp_path.parent / "other_pane"
    other.mkdir(exist_ok=True)

    from textual.widgets import DataTable
    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        panel = app._panel("left")
        panel.move_cursor_to("middle.py")
        await pilot.pause()
        table = panel.query_one(DataTable)
        row_before = table.cursor_row
        assert panel.selected_name() == "middle.py"

        await pilot.press("v")
        await pilot.pause(0.2)
        assert isinstance(app.screen, PreviewScreen)

        ta = app.screen.query_one("#preview-text", TextArea)
        assert ta.read_only is True
        assert "\x1b" not in ta.text
        assert "print('safe')" in ta.text

        # Typing inside read-only viewer does not modify text; w toggles soft wrap
        before_wrap = ta.soft_wrap
        await pilot.press("x")
        assert "print('safe')" in ta.text
        await pilot.press("w")
        await pilot.pause()
        assert ta.soft_wrap is (not before_wrap)

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"
        assert panel.selected_name() == "middle.py"
        assert table.cursor_row == row_before


@pytest.mark.asyncio
async def test_preview_screen_markdown_renders_and_toggles_source(tmp_path):
    md = tmp_path / "doc.md"
    md.write_text("# Title\n\n- item 1\n", encoding="utf-8")
    other = tmp_path.parent / "other_pane"
    other.mkdir(exist_ok=True)

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        app._panel("left").move_cursor_to("doc.md")
        await pilot.press("v")
        await pilot.pause(0.2)
        assert isinstance(app.screen, PreviewScreen)

        md_widget = app.screen.query_one("#preview-markdown", Markdown)
        assert md_widget._open_links is False

        await pilot.press("m")
        await pilot.pause()
        ta = app.screen.query_one("#preview-text", TextArea)
        assert ta.read_only is True
        assert "# Title" in ta.text

        await pilot.press("m")
        await pilot.pause()
        assert app.screen.query_one("#preview-markdown", Markdown) is not None

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.skipif(
    not preview.is_image_engine_available(),
    reason="optional [images] dependencies not installed",
)
@pytest.mark.asyncio
async def test_preview_screen_image_renders_and_cycles_modes(tmp_path):
    from PIL import Image as PILImage

    img = tmp_path / "pic.png"
    PILImage.new("RGB", (80, 60), color=(30, 180, 90)).save(img, format="PNG")
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        app._panel("left").move_cursor_to("pic.png")
        await pilot.press("v")
        await pilot.pause(0.3)
        assert isinstance(app.screen, PreviewScreen)
        assert app.screen.query_one("#preview-image") is not None
        assert app.screen.image_mode_index == 0

        await pilot.press("f")
        await pilot.pause(0.2)
        assert app.screen.image_mode_index == 1
        assert app.screen.query_one("#preview-image") is not None

        await pilot.press("f")
        await pilot.pause(0.2)
        assert app.screen.image_mode_index == 2
        assert app.screen.query_one("#preview-image") is not None

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.asyncio
async def test_preview_screen_image_without_engine_shows_metadata_fallback(tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(_minimal_png(90, 45))
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    with patch.object(preview, "is_image_engine_available", return_value=False):
        async with app.run_test(size=(140, 30)) as pilot:
            await _skip_boot(pilot)
            app._panel("left").move_cursor_to("pic.png")
            await pilot.press("v")
            await pilot.pause(0.2)
            assert isinstance(app.screen, PreviewScreen)
            meta = app.screen.query_one("#preview-meta", Static)
            rendered = str(meta.content)
            assert "90 x 45 px" in rendered
            assert "commandertui[images]" in rendered


@pytest.mark.asyncio
async def test_preview_screen_unsupported_file_shows_metadata(tmp_path):
    legacy = tmp_path / "report.ods"
    legacy.write_bytes(b"PK\x03\x04dummy\n")
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)
        app._panel("left").move_cursor_to("report.ods")
        await pilot.press("v")
        await pilot.pause(0.25)
        assert isinstance(app.screen, PreviewScreen)
        meta = app.screen.query_one("#preview-meta", Static)
        assert "OpenDocument" in str(meta.content)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.asyncio
async def test_preview_screen_discards_stale_result_after_esc(tmp_path):
    doc = tmp_path / "slow.txt"
    doc.write_text("slow content")
    other = tmp_path / "other"
    other.mkdir()

    started = threading.Event()
    release = threading.Event()

    def slow_load(path, cancel=None, **_kwargs):
        started.set()
        release.wait(timeout=2.0)
        return preview.load_preview(path, cancel=cancel)

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    with patch.object(preview, "load_preview_isolated", side_effect=slow_load):
        async with app.run_test(size=(140, 30)) as pilot:
            await _skip_boot(pilot)
            app._panel("left").move_cursor_to("slow.txt")
            await pilot.press("v")
            await pilot.pause(0.05)
            assert isinstance(app.screen, PreviewScreen)
            started.wait(timeout=1.0)
            await pilot.press("escape")
            await pilot.pause()
            release.set()
            await pilot.pause(0.1)
            assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.asyncio
async def test_macos_quicklook_alternative_from_viewer_and_setting(tmp_path, monkeypatch):
    doc = tmp_path / "file.txt"
    doc.write_text("hello")
    other = tmp_path / "other"
    other.mkdir()

    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "cfg" / "settings.json"))

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    with (
        patch.object(preview, "is_macos", return_value=True),
        patch.object(preview, "toggle_preview") as mock_toggle,
    ):
        async with app.run_test(size=(140, 30)) as pilot:
            await _skip_boot(pilot)
            app._panel("left").move_cursor_to("file.txt")

            # Default mode: v opens integrated PreviewScreen; o launches Quick Look inside it
            await pilot.press("v")
            await pilot.pause(0.2)
            assert isinstance(app.screen, PreviewScreen)
            await pilot.press("o")
            await pilot.pause()
            mock_toggle.assert_called_once_with(str(doc))

            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "MainScreen"

            # Configured mode: quicklook makes v invoke Quick Look directly on macOS
            mock_toggle.reset_mock()
            settings.save_preview_mode("quicklook")
            await pilot.press("v")
            await pilot.pause()
            mock_toggle.assert_called_once_with(str(doc))
            assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.skipif(
    not preview.is_image_engine_available(),
    reason="optional [images] dependencies not installed",
)
def test_load_preview_decompression_bomb_png_returns_metadata(tmp_path):
    bomb_png = tmp_path / "bomb.png"
    bomb_png.write_bytes(_minimal_png(100_000, 100_000))

    res = preview.load_preview(str(bomb_png))
    assert res.kind == PreviewKind.METADATA
    assert res.pil_image is None
    assert res.metadata.image_dimensions == (100_000, 100_000)
    assert "pixel limit" in (res.note or "").lower()

    # Also verify direct catch when header parser cannot pre-inspect dimensions
    from PIL import Image as PILImage

    with (
        patch.object(preview, "parse_image_dimensions", return_value=None),
        patch.object(PILImage, "open", side_effect=PILImage.DecompressionBombError("boom")),
    ):
        res_err = preview.load_preview(str(bomb_png))
    assert res_err.kind == PreviewKind.METADATA
    assert res_err.pil_image is None
    assert "pixel limit" in (res_err.note or "").lower()


@pytest.mark.skipif(
    not preview.is_image_engine_available(),
    reason="optional [images] dependencies not installed",
)
def test_image_widget_uses_sixel_image_when_terminal_supports_sixel():
    script = """
import sys
import textual_image._terminal as ti_term

class _TTY:
    encoding = "utf-8"
    def isatty(self):
        return True
    def write(self, data):
        return len(data)
    def flush(self):
        pass

sys.__stdout__ = _TTY()
ti_term.probe_terminal._result = ti_term.TerminalCapabilities(
    ti_term.CellSize(10, 20), sixel=True, tgp=False
)

from PIL import Image as PILImage
from textual_image.widget import SixelImage
from commandertui.preview import FileMetadata, PreviewKind, PreviewResult
from commandertui.preview_screen import PreviewScreen

screen = PreviewScreen("/tmp/sample.png")
meta = FileMetadata(
    name="sample.png",
    path="/tmp/sample.png",
    size_bytes=12,
    mtime=None,
    ctime=None,
    mode_str="-rw-r--r--",
    octal_mode="644",
    is_symlink=False,
    symlink_target=None,
    file_type="Image",
    mime_type="image/png",
    image_dimensions=(4, 4),
)
res = PreviewResult(
    kind=PreviewKind.IMAGE,
    path="/tmp/sample.png",
    metadata=meta,
    pil_image=PILImage.new("RGB", (4, 4), (10, 20, 30)),
    image_engine_available=True,
)
widget = screen._build_image_widget(res)
assert isinstance(widget, SixelImage), f"Expected SixelImage, got {type(widget)!r}"
"""
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"


@pytest.mark.skipif(
    not preview.is_image_engine_available(),
    reason="optional [images] dependencies not installed",
)
def test_load_preview_image_decode_timeout_stops_slow_transpose(tmp_path):
    from PIL import Image as PILImage
    from PIL import ImageOps

    img_path = tmp_path / "slow.png"
    PILImage.new("RGB", (16, 16), (20, 40, 60)).save(img_path)

    orig_transpose = ImageOps.exif_transpose

    def slow_transpose(im):
        time.sleep(0.15)
        return orig_transpose(im)

    with patch.object(ImageOps, "exif_transpose", side_effect=slow_transpose):
        res = preview.load_preview(str(img_path), image_timeout=0.01)

    assert res.kind == PreviewKind.ERROR
    assert res.pil_image is None
    assert res.error and "timed out" in res.error.lower()


def test_load_preview_isolated_terminates_blocked_read_on_timeout_and_cancel(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(
        "import builtins, os, time\n"
        "_orig_open = builtins.open\n"
        "def _slow_open(file, *args, **kwargs):\n"
        "    if os.path.basename(os.fspath(file)) == 'blocked.txt':\n"
        "        time.sleep(5.0)\n"
        "    return _orig_open(file, *args, **kwargs)\n"
        "builtins.open = _slow_open\n",
        encoding="utf-8",
    )
    ok_file = tmp_path / "ok.txt"
    ok_file.write_text("isolated ok\n", encoding="utf-8")
    blocked_file = tmp_path / "blocked.txt"
    blocked_file.write_text("never read", encoding="utf-8")

    existing_pp = os.environ.get("PYTHONPATH")
    new_pp = f"{hook_dir}{os.pathsep}{existing_pp}" if existing_pp else str(hook_dir)
    monkeypatch.setenv("PYTHONPATH", new_pp)

    ok_res = preview.load_preview_isolated(str(ok_file), wall_timeout=5.0)
    assert ok_res.kind == PreviewKind.TEXT
    assert ok_res.text == "isolated ok\n"

    t0 = time.monotonic()
    timeout_res = preview.load_preview_isolated(str(blocked_file), wall_timeout=0.25)
    elapsed_timeout = time.monotonic() - t0
    assert timeout_res.kind == PreviewKind.ERROR
    assert timeout_res.error and "timed out" in timeout_res.error.lower()
    assert elapsed_timeout < 1.5

    cancel_event = threading.Event()
    timer = threading.Timer(0.08, cancel_event.set)
    timer.start()
    try:
        t1 = time.monotonic()
        cancel_res = preview.load_preview_isolated(
            str(blocked_file),
            cancel=cancel_event.is_set,
            wall_timeout=5.0,
        )
        elapsed_cancel = time.monotonic() - t1
    finally:
        timer.cancel()

    assert cancel_res.cancelled is True
    assert elapsed_cancel < 1.5


@pytest.mark.asyncio
async def test_main_screen_preview_does_not_stat_or_isdir_on_ui_thread(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    doc = tmp_path / "note.txt"
    doc.write_text("hello UI thread\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app.run_test(size=(140, 30)) as pilot:
        await _skip_boot(pilot)

        ui_fs_calls: list[tuple[str, str]] = []
        orig_isdir = os.path.isdir
        orig_stat = os.stat
        orig_lstat = os.lstat
        main_thread = threading.main_thread()

        def spy_isdir(p, *args, **kwargs):
            if threading.current_thread() is main_thread:
                ui_fs_calls.append(("isdir", os.fspath(p)))
            return orig_isdir(p, *args, **kwargs)

        def spy_stat(p, *args, **kwargs):
            if threading.current_thread() is main_thread:
                ui_fs_calls.append(("stat", os.fspath(p)))
            return orig_stat(p, *args, **kwargs)

        def spy_lstat(p, *args, **kwargs):
            if threading.current_thread() is main_thread:
                ui_fs_calls.append(("lstat", os.fspath(p)))
            return orig_lstat(p, *args, **kwargs)

        app._panel("left").move_cursor_to("folder")
        with (
            patch("os.path.isdir", side_effect=spy_isdir),
            patch("os.stat", side_effect=spy_stat),
            patch("os.lstat", side_effect=spy_lstat),
        ):
            await pilot.press("v")
            await pilot.pause()

        assert not [c for c in ui_fs_calls if c[1] == str(folder)]
        assert isinstance(app.screen, MessageScreen)
        await pilot.press("enter")
        await pilot.pause()

        ui_fs_calls.clear()
        app._panel("left").move_cursor_to("note.txt")
        with (
            patch("os.path.isdir", side_effect=spy_isdir),
            patch("os.stat", side_effect=spy_stat),
            patch("os.lstat", side_effect=spy_lstat),
        ):
            await pilot.press("v")
            await pilot.pause()

        assert not [c for c in ui_fs_calls if c[1] == str(doc)]
        assert isinstance(app.screen, PreviewScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.skipif(
    not preview.is_image_engine_available(),
    reason="optional [images] dependencies not installed",
)
def test_tgp_backend_large_window_height_and_resize_stay_bounded_in_clean_subprocess(tmp_path):
    from PIL import Image as PILImage

    wide_path = tmp_path / "wide.png"
    tall_path = tmp_path / "tall.png"
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    PILImage.new("RGB", (1600, 480), (40, 120, 200)).save(wide_path)
    PILImage.new("RGB", (200, 1600), (200, 120, 40)).save(tall_path)

    script = f"""
import asyncio
import sys
import textual_image._terminal as ti_term

class _TTY:
    encoding = "utf-8"
    def __init__(self):
        self.chunks = []
    def isatty(self):
        return True
    def write(self, data):
        self.chunks.append(data)
        return len(data)
    def flush(self):
        pass

tty = _TTY()
sys.__stdout__ = tty
ti_term.probe_terminal._result = ti_term.TerminalCapabilities(
    ti_term.CellSize(8, 18), sixel=False, tgp=True
)

import textual_image.widget as tiw
from textual_image.renderable.tgp import Image as TGPRenderable
assert tiw.Image._Renderable is TGPRenderable, f"Expected TGP renderable, got {{tiw.Image._Renderable!r}}"

from commandertui.app import CommanderApp

async def skip_boot(pilot):
    await pilot.pause()
    for _ in range(4):
        await pilot.press("enter")
        await pilot.pause()
        if pilot.app.screen.__class__.__name__ != "BootScreen":
            break

async def main():
    # 1) Exact 310x111 window (container width > 297) + resize small <-> large
    app1 = CommanderApp(left_path={str(tmp_path)!r}, right_path={str(other_dir)!r}, check_updates=False)
    async with app1.run_test(size=(80, 24)) as pilot:
        await skip_boot(pilot)
        app1._panel("left").move_cursor_to("wide.png")
        await pilot.press("v")
        await pilot.pause(0.4)
        img_w = app1.screen.query_one("#preview-image")
        small = img_w.size
        assert 0 < small.width <= 76 and 0 < small.height <= 18, f"small={{small}}"

        await pilot.resize_terminal(310, 111)
        await pilot.pause(0.4)
        big = img_w.size
        assert big.width > small.width, f"Did not expand on resize: {{small}} -> {{big}}"
        assert 0 < big.width <= 256 and 0 < big.height <= 256, f"Exceeded cell cap: {{big}}"

        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.3)
        back = img_w.size
        assert 0 < back.width <= 76 and 0 < back.height <= 18, f"Did not shrink back: {{back}}"

        await pilot.press("escape")
        await pilot.pause()
        assert app1.screen.__class__.__name__ == "MainScreen"

    # 2) Tall viewport > 297 rows (350x350) with tall portrait image checks height cap too
    app2 = CommanderApp(left_path={str(tmp_path)!r}, right_path={str(other_dir)!r}, check_updates=False)
    async with app2.run_test(size=(350, 350)) as pilot:
        await skip_boot(pilot)
        app2._panel("left").move_cursor_to("tall.png")
        await pilot.press("v")
        await pilot.pause(0.4)
        tall_size = app2.screen.query_one("#preview-image").size
        assert 0 < tall_size.width <= 256 and 0 < tall_size.height <= 256, f"tall_size={{tall_size}}"
        await pilot.press("escape")
        await pilot.pause()
        assert app2.screen.__class__.__name__ == "MainScreen"

    assert any("\\x1b_G" in c for c in tty.chunks), "Real TGP render never emitted graphics payload"

asyncio.run(main())
"""
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"


@pytest.mark.skipif(
    not preview.is_image_engine_available(),
    reason="optional [images] dependencies not installed",
)
@pytest.mark.asyncio
async def test_deferred_image_render_error_falls_back_to_halfcell_without_crashing(tmp_path):
    from PIL import Image as PILImage
    from textual.widgets import Static

    preview.ensure_safe_image_terminal()
    from textual_image.renderable.tgp import Image as TGPRenderable
    import textual_image.widget as tiw

    class _TGPAutoImage(tiw.AutoImage.__mro__[1], Renderable=TGPRenderable):
        pass

    img_path = tmp_path / "photo.png"
    PILImage.new("RGB", (600, 400), (10, 80, 160)).save(img_path)
    other = tmp_path / "other"
    other.mkdir()

    from commandertui.app import CommanderApp

    app = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    with (
        patch.object(tiw, "Image", _TGPAutoImage),
        patch.object(TGPRenderable, "__rich_console__", side_effect=ValueError("Image to large to render")),
    ):
        async with app.run_test(size=(120, 30)) as pilot:
            await _skip_boot(pilot)
            app._panel("left").move_cursor_to("photo.png")
            await pilot.press("v")
            await pilot.pause(0.4)
            assert isinstance(app.screen, PreviewScreen)
            notice = app.screen.query_one("#preview-notice", Static)
            assert notice.display is True
            assert "halfcell" in str(notice.content).lower()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "MainScreen"


def test_settings_image_mode_persistence_and_herdr_default(monkeypatch, tmp_path):
    from commandertui import settings

    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    # Non-Herdr terminal with generic TERM/HOSTNAME -> default is auto
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "Ghostty")
    assert settings.is_herdr_session() is False
    assert settings.load_image_mode() is None
    assert settings.default_image_mode() == "auto"

    # Simulated Herdr env vars -> default is halfcell when unset
    for env_key, env_val in (
        ("HERDR_ENV", "1"),
        ("HERDR_PANE_ID", "w3:p1"),
        ("HERDR_SOCKET_PATH", "/tmp/herdr.sock"),
        ("TERM_PROGRAM", "herdr"),
    ):
        with monkeypatch.context() as mc:
            mc.setenv(env_key, env_val)
            assert settings.is_herdr_session() is True
            assert settings.default_image_mode() == "halfcell"

    # Explicit saved choice overrides Herdr default
    monkeypatch.setenv("HERDR_ENV", "1")
    settings.save_image_mode("unicode")
    assert settings.load_image_mode() == "unicode"
    assert settings.default_image_mode() == "unicode"

    settings.save_image_mode("auto")
    assert settings.default_image_mode() == "auto"

    with pytest.raises(ValueError):
        settings.save_image_mode("invalid")


@pytest.mark.skipif(
    not preview.is_image_engine_available(),
    reason="optional [images] dependencies not installed",
)
@pytest.mark.asyncio
async def test_preview_screen_image_mode_persists_herdr_default_and_prev_label(monkeypatch, tmp_path):
    from PIL import Image as PILImage
    from textual.widgets import Static

    from commandertui import settings
    from commandertui.app import CommanderApp
    from commandertui.preview_screen import PreviewBar
    from commandertui.widgets import FnButton

    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "cfg" / "settings.json"))
    pic1 = tmp_path / "pic1.png"
    pic2 = tmp_path / "pic2.png"
    PILImage.new("RGB", (40, 30), (20, 100, 200)).save(pic1)
    PILImage.new("RGB", (40, 30), (200, 100, 20)).save(pic2)
    other = tmp_path / "other"
    other.mkdir()

    # 1. Herdr environment starts in halfcell by default and shows f hint
    monkeypatch.setenv("HERDR_ENV", "1")
    app1 = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app1.run_test(size=(120, 28)) as pilot:
        await _skip_boot(pilot)
        app1._panel("left").move_cursor_to("pic1.png")
        await pilot.press("v")
        await pilot.pause(0.3)
        assert isinstance(app1.screen, PreviewScreen)
        assert app1.screen.image_mode_index == 1  # halfcell
        assert "IMAGE (HALFCELL)" in str(app1.screen.query_one("#preview-header", Static).content)
        notice = app1.screen.query_one("#preview-notice", Static)
        assert notice.display is True
        assert "press f" in str(notice.content).lower()

        # Verify Prev/Next literal '[' label renders cleanly (no literal '[[/b]')
        bar = app1.screen.query_one("#preview-bar", PreviewBar)
        bar.update_controls(show_pager=True, can_prev=True, can_next=True, pager_noun="Page")
        prev_btn = next(b for b in bar.query(FnButton) if b.action_name == "prev_item")
        assert "[[/b]" not in str(prev_btn.label)
        assert str(prev_btn.label) == "[\u00a0Prev Page"

        # Cycle from halfcell -> unicode and exit back
        await pilot.press("f")
        await pilot.pause(0.2)
        assert app1.screen.image_mode_index == 2  # unicode
        await pilot.press("escape")
        await pilot.pause()

        # Next preview in same app remembers unicode
        app1._panel("left").move_cursor_to("pic2.png")
        await pilot.press("v")
        await pilot.pause(0.3)
        assert app1.screen.image_mode_index == 2
        assert "IMAGE (UNICODE)" in str(app1.screen.query_one("#preview-header", Static).content)
        await pilot.press("escape")
        await pilot.pause()

    # 2. New CommanderApp instance (simulated restart) preserves saved unicode even in Herdr
    app2 = CommanderApp(left_path=str(tmp_path), right_path=str(other), check_updates=False)
    async with app2.run_test(size=(120, 28)) as pilot:
        await _skip_boot(pilot)
        app2._panel("left").move_cursor_to("pic1.png")
        await pilot.press("v")
        await pilot.pause(0.3)
        assert app2.screen.image_mode_index == 2
        await pilot.press("escape")
        await pilot.pause()
