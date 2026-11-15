"""Full-screen on-demand file preview viewer for CommanderTUI."""

from __future__ import annotations

import os
import re
import threading
import time

from rich.console import Console, ConsoleOptions, RenderResult
from rich.measure import Measurement
from rich.segment import Segment
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical, VerticalScroll
from textual.geometry import Size
from textual.screen import Screen
from textual.widgets import DataTable, Markdown, Static, TextArea

from . import preview, settings
from .modals import ChoiceScreen
from .preview import (
    FileMetadata,
    PreviewKind,
    PreviewResult,
    format_size_bytes,
    format_timestamp,
    sanitize_terminal_text,
)
from .widgets import SPINNER_FRAMES, ActionBar, FnButton

IMAGE_MODES = settings.IMAGE_MODES
# TGP diacritic index table in textual_image has 297 entries; cap safely below it on both axes.
MAX_IMAGE_RENDER_CELLS = 256
# Textual's Markdown widget mounts one widget per block (paragraph, list item, table row) on the
# UI thread: a few thousand blocks freeze the app for tens of seconds. Above this estimate the
# file is shown as source text instead (about 2 seconds of rendering at the limit).
MAX_MARKDOWN_RENDER_BLOCKS = 300
_MD_CHUNK_RE = re.compile(r"\n[ \t]*\n")
_MD_ITEM_RE = re.compile(r"(?m)^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\S|^[ \t]*\|")


def estimate_markdown_blocks(text: str) -> int:
    """Cheap estimate of how many widgets Textual's Markdown would mount for `text`."""
    chunks = sum(1 for chunk in _MD_CHUNK_RE.split(text) if chunk.strip())
    return chunks + len(_MD_ITEM_RE.findall(text))


def _is_paged(result: PreviewResult | None) -> bool:
    """True for results the user navigates with [ ] (PDF pages, workbook sheets)."""
    if result is None:
        return False
    if result.kind == PreviewKind.PDF:
        return result.page_count > 0
    return result.kind == PreviewKind.TABLE and bool(result.sheet_names) and len(result.sheet_names) > 1


def _bound_console_options(options: ConsoleOptions, max_cells: int) -> ConsoleOptions:
    bounded = options.copy()
    bounded.max_width = max(1, min(options.max_width, max_cells))
    bounded.min_width = min(options.min_width, bounded.max_width)
    bounded.max_height = max(1, min(options.max_height, max_cells))
    if bounded.height is not None:
        bounded.height = max(1, min(bounded.height, max_cells))
    return bounded


class _SafeImageRenderable:
    """Bounds Rich render options in cells and falls back to Halfcell on deferred render errors."""

    def __init__(
        self,
        inner: object,
        *,
        pil_image: object,
        max_cells: int,
        fallback_cls: type,
        on_fallback=None,
    ) -> None:
        self._inner = inner
        self._pil_image = pil_image
        self._max_cells = max_cells
        self._fallback_cls = fallback_cls
        self._on_fallback = on_fallback
        self._notified = False

    def cleanup(self) -> None:
        try:
            self._inner.cleanup()
        except Exception:
            pass

    def _notify_fallback(self, exc: Exception) -> None:
        if not self._notified and self._on_fallback is not None:
            self._notified = True
            try:
                self._on_fallback(exc)
            except Exception:
                pass

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        bounded = _bound_console_options(options, self._max_cells)
        try:
            meas = self._inner.__rich_measure__(console, bounded)
            return Measurement(min(meas.minimum, self._max_cells), min(meas.maximum, self._max_cells))
        except Exception:
            return Measurement(1, 1)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        bounded = _bound_console_options(options, self._max_cells)
        try:
            segments = list(self._inner.__rich_console__(console, bounded))
            yield from segments
            return
        except Exception as exc:
            self._notify_fallback(exc)
            try:
                fallback = self._fallback_cls(self._pil_image, "auto", "auto")
                fb_segments = list(fallback.__rich_console__(console, bounded))
                yield from fb_segments
                return
            except Exception as fb_exc:
                yield Segment(f"Image preview error: {sanitize_terminal_text(str(fb_exc))}")


def _make_safe_image_widget_class(
    base_cls: type,
    *,
    fallback_renderable_cls: type,
    max_cells: int = MAX_IMAGE_RENDER_CELLS,
    on_render_fallback=None,
) -> type:
    renderable_cls = getattr(base_cls, "_Renderable", None)
    if renderable_cls is None:
        return base_cls

    class _SafeBoundedImageWidget(base_cls, Renderable=renderable_cls):  # type: ignore[misc,call-arg]
        def _bounded_budget(
            self, container: Size, viewport: Size, *, override_width: int | None = None
        ) -> Size:
            if override_width is not None:
                avail_w = min(max(1, override_width), max_cells)
            elif container.width > 0:
                avail_w = min(container.width, max_cells)
            else:
                avail_w = min(max(1, viewport.width or max_cells), max_cells)

            if container.height > 0:
                avail_h = min(container.height, max_cells)
            else:
                scroll_h = 0
                try:
                    parent = self.parent
                    grandparent = parent.parent if parent is not None else None
                    if grandparent is not None and grandparent.size.height > 2:
                        scroll_h = grandparent.size.height - 2
                except Exception:
                    scroll_h = 0
                if scroll_h > 0:
                    avail_h = min(scroll_h, max_cells)
                else:
                    vh = viewport.height or max_cells
                    avail_h = min(max(1, vh - 6 if vh > 6 else vh), max_cells)
            return Size(avail_w, avail_h)

        def get_content_width(self, container: Size, viewport: Size) -> int:
            bounded = self._bounded_budget(container, viewport)
            return min(max_cells, super().get_content_width(bounded, bounded))

        def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
            bounded = self._bounded_budget(container, viewport, override_width=width)
            return min(max_cells, super().get_content_height(bounded, bounded, bounded.width))

        def render(self):
            try:
                rendered = super().render()
            except Exception as exc:
                if on_render_fallback is not None:
                    try:
                        on_render_fallback(exc)
                    except Exception:
                        pass
                try:
                    rendered = fallback_renderable_cls(self._image, "auto", "auto")
                except Exception as fb_exc:
                    return f"Image preview error: {sanitize_terminal_text(str(fb_exc))}"
            if isinstance(rendered, str) or type(rendered).__name__ == "_NoopRenderable":
                return rendered
            return _SafeImageRenderable(
                rendered,
                pil_image=self._image,
                max_cells=max_cells,
                fallback_cls=fallback_renderable_cls,
                on_fallback=on_render_fallback,
            )

        def on_unmount(self) -> None:
            if getattr(self, "_renderable", None) is not None:
                try:
                    self._renderable.cleanup()
                except Exception:
                    self._renderable = None

    return _SafeBoundedImageWidget


class PreviewBar(ActionBar):
    """Bottom action bar for PreviewScreen."""

    PREFIX = "pb"
    KEYS = [
        ("Esc", "Back", "back"),
        ("w", "Wrap", "toggle_wrap"),
        ("m", "Source", "toggle_markdown_source"),
        ("f", "Img mode", "cycle_image_mode"),
        ("g", "Visual", "toggle_pdf_visual"),
        ("[", "Prev", "prev_item"),
        ("]", "Next", "next_item"),
        ("s", "Sheet", "choose_sheet"),
        ("o", "Open", "quick_look"),
    ]

    def update_controls(
        self,
        *,
        show_wrap: bool = False,
        show_markdown: bool = False,
        markdown_source: bool = False,
        show_image_mode: bool = False,
        image_mode: str = "auto",
        show_pdf_visual: bool = False,
        pdf_visual_mode: bool = False,
        show_pager: bool = False,
        can_prev: bool = False,
        can_next: bool = False,
        pager_noun: str = "Page",
        show_sheet_picker: bool = False,
        show_quick_look: bool = False,
        quick_look_label: str = "Open",
    ) -> None:
        for btn in self.query(FnButton):
            action = getattr(btn, "action_name", "")
            if action == "back":
                btn.display = True
            elif action == "toggle_wrap":
                btn.display = show_wrap
            elif action == "toggle_markdown_source":
                btn.display = show_markdown
                label = "Rendered" if markdown_source else "Source"
                btn.label = Text.assemble(("m", "bold"), f"\u00a0{label}")
            elif action == "cycle_image_mode":
                btn.display = show_image_mode
                btn.label = Text.assemble(("f", "bold"), f"\u00a0Mode:{image_mode}")
            elif action == "toggle_pdf_visual":
                btn.display = show_pdf_visual
                label = "Text" if pdf_visual_mode else "Visual"
                btn.label = Text.assemble(("g", "bold"), f"\u00a0{label}")
            elif action == "prev_item":
                btn.display = show_pager
                btn.disabled = not can_prev
                btn.label = Text.assemble(("[", "bold"), f"\u00a0Prev {pager_noun}")
            elif action == "next_item":
                btn.display = show_pager
                btn.disabled = not can_next
                btn.label = Text.assemble(("]", "bold"), f"\u00a0Next {pager_noun}")
            elif action == "choose_sheet":
                btn.display = show_sheet_picker
            elif action == "quick_look":
                btn.display = show_quick_look
                btn.label = Text.assemble(("o", "bold"), f"\u00a0{quick_look_label}")


def _format_metadata_lines(meta: FileMetadata, *, note: str | None = None, error: str | None = None) -> str:
    lines = [
        f"Name:        {meta.name}",
        f"Path:        {meta.path}",
        f"Type:        {meta.file_type}",
        f"MIME:        {meta.mime_type}",
        f"Size:        {format_size_bytes(meta.size_bytes)}",
        f"Modified:    {format_timestamp(meta.mtime)}",
        f"Permissions: {meta.mode_str} ({meta.octal_mode})",
    ]
    if meta.is_symlink:
        lines.append(f"Symlink ->   {meta.symlink_target or '(unknown)'}")
    if meta.image_dimensions is not None:
        w, h = meta.image_dimensions
        lines.append(f"Dimensions:  {w} x {h} px")
    if note:
        lines.extend(["", f"Note: {sanitize_terminal_text(note)}"])
    if error:
        lines.extend(["", f"Error: {sanitize_terminal_text(error)}"])
    return "\n".join(lines)


class PreviewScreen(Screen):
    """On-demand large file preview screen (Esc returns to the file list)."""

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("w", "toggle_wrap", "Wrap", priority=True),
        Binding("m", "toggle_markdown_source", "Markdown source", priority=True),
        Binding("f", "cycle_image_mode", "Image mode", priority=True),
        Binding("g", "toggle_pdf_visual", "PDF visual", priority=True),
        Binding("left_square_bracket", "prev_item", "Previous page or sheet", priority=True),
        Binding("right_square_bracket", "next_item", "Next page or sheet", priority=True),
        Binding("s", "choose_sheet", "Select sheet", priority=True),
        Binding("o", "quick_look", "Open with system viewer", priority=True),
    ]

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = os.path.abspath(path)
        self.result: PreviewResult | None = None
        self.soft_wrap = True
        self.markdown_source = False
        self.markdown_too_large = False
        self.image_mode_index = IMAGE_MODES.index(settings.default_image_mode())
        self.page_index = 0
        self.sheet_index = 0
        self.pdf_visual = False
        self._load_generation = 0
        self._cancel = threading.Event()
        self._spinner = None
        self._loading = False

    def compose(self) -> ComposeResult:
        safe_name = sanitize_terminal_text(os.path.basename(self.path) or self.path)
        yield Static(f"PREVIEW: {safe_name}", id="preview-header", markup=False)
        yield Static("", id="preview-notice", markup=False)
        with Vertical(id="preview-body"):
            yield Static("Loading preview...", id="preview-loading", markup=False)
        yield PreviewBar(id="preview-bar")

    def on_mount(self) -> None:
        notice = self.query_one("#preview-notice", Static)
        notice.display = False
        self._sync_bar()
        self._start_load()

    def on_unmount(self) -> None:
        self._cancel.set()
        self._load_generation += 1
        self._stop_spinner()

    def _stop_spinner(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    def _start_load(self) -> None:
        self._cancel.set()
        self._load_generation += 1
        generation = self._load_generation
        cancel = self._cancel = threading.Event()
        self._loading = True
        self._stop_spinner()

        loading_matches = list(self.query("#preview-loading"))
        status_widget = (
            (
                loading_matches[0]
                if isinstance(loading_matches[0], Static)
                else self.query_one("#preview-notice", Static)
            )
            if loading_matches
            else self.query_one("#preview-notice", Static)
        )
        if not loading_matches:
            status_widget.display = True
        started = time.monotonic()
        frame = 0
        safe_path = sanitize_terminal_text(self.path)
        req_page = self.page_index
        req_sheet = self.sheet_index
        req_visual = self.pdf_visual

        def spin() -> None:
            nonlocal frame
            frame += 1
            elapsed = int(time.monotonic() - started)
            status_widget.update(
                f"Loading {safe_path} {SPINNER_FRAMES[frame % 4]}  {elapsed}s  (Esc to cancel)"
            )

        spin()
        self._spinner = self.set_interval(0.12, spin)

        def work() -> None:
            try:
                res = preview.load_preview_isolated(
                    self.path,
                    cancel=cancel.is_set,
                    page_index=req_page,
                    pdf_visual=req_visual,
                    sheet_index=req_sheet,
                )
            except Exception as exc:
                res = PreviewResult(
                    kind=PreviewKind.ERROR,
                    path=os.path.abspath(self.path),
                    metadata=preview._minimal_fallback_metadata(os.path.abspath(self.path)),
                    error=f"Preview worker error: {sanitize_terminal_text(str(exc))}",
                )
            if not cancel.is_set():
                try:
                    self.app.call_from_thread(self._on_loaded, generation, cancel, res)
                except Exception:
                    pass

        self.run_worker(work, thread=True)

    async def _on_loaded(self, generation: int, cancel: threading.Event, result: PreviewResult) -> None:
        if generation != self._load_generation or cancel.is_set() or result.cancelled or not self.is_mounted:
            return
        self._loading = False
        self._stop_spinner()
        previous = self.result
        if _is_paged(previous) and result.kind in (PreviewKind.ERROR, PreviewKind.METADATA):
            # A page, sheet or visual-mode switch that failed must not strand the viewer on an
            # error view without navigation: keep what was on screen and say what went wrong.
            if previous.kind == PreviewKind.PDF:
                what = f"page {self.page_index + 1}"
                if self.pdf_visual and not previous.pdf_visual_mode:
                    what += " (visual)"
            else:
                what = f"sheet {self.sheet_index + 1}"
            self.page_index = previous.page_index
            self.sheet_index = previous.sheet_index
            self.pdf_visual = previous.pdf_visual_mode
            reason = sanitize_terminal_text(result.error or result.note or "preview failed")
            notice = self.query_one("#preview-notice", Static)
            notice.update(f"Could not load {what}: {reason}")
            notice.display = True
            self._sync_bar()
            return
        self.result = result
        if result.kind == PreviewKind.MARKDOWN:
            self.markdown_too_large = estimate_markdown_blocks(result.text or "") > MAX_MARKDOWN_RENDER_BLOCKS
            if self.markdown_too_large:
                self.markdown_source = True
        self.page_index = result.page_index
        self.sheet_index = result.sheet_index
        if result.kind == PreviewKind.PDF:
            self.pdf_visual = result.pdf_visual_mode
        await self._render_result_safely()

    async def _render_result_safely(self) -> None:
        try:
            await self._render_result()
        except Exception as exc:
            await self._render_error_fallback(exc)

    async def _render_error_fallback(self, exc: Exception) -> None:
        if not self.is_mounted:
            return
        err_msg = f"Preview display error: {sanitize_terminal_text(str(exc))}"
        meta = (
            self.result.metadata
            if self.result is not None
            else preview._minimal_fallback_metadata(os.path.abspath(self.path))
        )
        fallback = PreviewResult(
            kind=PreviewKind.ERROR,
            path=meta.path,
            metadata=meta,
            error=err_msg,
        )
        self.result = fallback
        self.pdf_visual = False
        try:
            self.query_one("#preview-header", Static).update(f"PREVIEW [ERROR]: {meta.name}")
        except Exception:
            pass
        try:
            notice = self.query_one("#preview-notice", Static)
            notice.update(err_msg)
            notice.display = True
        except Exception:
            pass
        try:
            self._sync_bar()
        except Exception:
            pass
        try:
            body = self.query_one("#preview-body", Vertical)
            await body.remove_children()
            meta_text = _format_metadata_lines(meta, error=err_msg)
            scroll = VerticalScroll(
                Static(meta_text, id="preview-meta", markup=False),
                id="preview-scroll",
            )
            await body.mount(scroll)
            scroll.focus()
        except Exception:
            pass

    def _sync_bar(self) -> None:
        bar = self.query_one("#preview-bar", PreviewBar)
        res = self.result
        is_text = res is not None and res.kind == PreviewKind.TEXT
        is_md = res is not None and res.kind == PreviewKind.MARKDOWN
        is_pdf = res is not None and res.kind == PreviewKind.PDF
        is_pdf_visual = (
            is_pdf and res.pdf_visual_mode and res.pil_image is not None and res.image_engine_available
        )
        is_img = (
            res is not None
            and res.kind == PreviewKind.IMAGE
            and res.pil_image is not None
            and res.image_engine_available
        )
        is_xlsx_multi = (
            res is not None
            and res.kind == PreviewKind.TABLE
            and res.sheet_names is not None
            and len(res.sheet_names) > 1
        )
        show_pager = (is_pdf and res.page_count > 1) or is_xlsx_multi
        can_prev = (
            (res.page_index > 0)
            if (is_pdf and res is not None)
            else (res.sheet_index > 0 if is_xlsx_multi and res is not None else False)
        )
        can_next = (
            (res.page_index + 1 < res.page_count)
            if (is_pdf and res is not None)
            else (
                (res.sheet_index + 1 < len(res.sheet_names))
                if is_xlsx_multi and res is not None and res.sheet_names is not None
                else False
            )
        )
        pager_noun = "Sheet" if is_xlsx_multi else "Page"
        bar.update_controls(
            show_wrap=is_text or (is_md and self.markdown_source) or (is_pdf and not is_pdf_visual),
            show_markdown=is_md and not self.markdown_too_large,
            markdown_source=self.markdown_source,
            show_image_mode=is_img or is_pdf_visual,
            image_mode=IMAGE_MODES[self.image_mode_index],
            show_pdf_visual=is_pdf
            and res.page_count > 0
            and (res.pdf_visual_available or res.pdf_visual_mode),
            pdf_visual_mode=bool(res and res.pdf_visual_mode),
            show_pager=show_pager,
            can_prev=can_prev,
            can_next=can_next,
            pager_noun=pager_noun,
            show_sheet_picker=is_xlsx_multi,
            show_quick_look=preview.is_macos() or preview.system_opener() is not None,
            quick_look_label="Quick Look" if preview.is_macos() else "Open",
        )

    def _update_header(self, result: PreviewResult) -> None:
        meta = result.metadata
        kind_tag = result.kind.value.upper()
        if result.kind == PreviewKind.MARKDOWN:
            kind_tag = "MARKDOWN (SOURCE)" if self.markdown_source else "MARKDOWN"
        elif result.kind == PreviewKind.IMAGE and result.pil_image is not None:
            kind_tag = f"IMAGE ({IMAGE_MODES[self.image_mode_index].upper()})"
        elif result.kind == PreviewKind.PDF:
            page_info = f"Page {result.page_index + 1}/{max(1, result.page_count)}"
            if result.pdf_visual_mode:
                kind_tag = f"PDF {page_info} VISUAL ({IMAGE_MODES[self.image_mode_index].upper()})"
            else:
                kind_tag = f"PDF {page_info}"
        elif result.kind == PreviewKind.TABLE:
            if result.sheet_names:
                sname = result.sheet_names[result.sheet_index]
                kind_tag = f"XLSX {result.sheet_index + 1}/{len(result.sheet_names)}: {sname}"
            else:
                kind_tag = meta.file_type.upper() if meta.file_type else "TABLE"

        parts = [f"PREVIEW [{kind_tag}]: {meta.name}"]
        if meta.size_bytes is not None:
            parts.append(format_size_bytes(meta.size_bytes))
        if meta.image_dimensions is not None:
            w, h = meta.image_dimensions
            parts.append(f"{w}x{h}")
        if result.encoding:
            parts.append(result.encoding)
        self.query_one("#preview-header", Static).update("  |  ".join(parts))

        notice_widget = self.query_one("#preview-notice", Static)
        notices = [msg for msg in (result.truncated_reason, result.note, result.error) if msg]
        if result.kind == PreviewKind.MARKDOWN and self.markdown_too_large:
            notices.append("Large Markdown file: showing source text (rendering it would freeze the viewer)")
        is_rendered_visual = (
            result.pil_image is not None
            and result.image_engine_available
            and (
                result.kind == PreviewKind.IMAGE
                or (result.kind == PreviewKind.PDF and result.pdf_visual_mode)
            )
        )
        if is_rendered_visual:
            notices.append("If blank, press f to change image mode")
        if notices and result.kind in (
            PreviewKind.TEXT,
            PreviewKind.MARKDOWN,
            PreviewKind.IMAGE,
            PreviewKind.PDF,
            PreviewKind.TABLE,
        ):
            notice_widget.update("  |  ".join(sanitize_terminal_text(n) for n in notices))
            notice_widget.display = True
        else:
            notice_widget.update("")
            notice_widget.display = False

    async def _render_result(self) -> None:
        result = self.result
        if result is None or not self.is_mounted:
            return

        self._update_header(result)
        self._sync_bar()

        body = self.query_one("#preview-body", Vertical)
        await body.remove_children()

        if result.kind == PreviewKind.TEXT or (result.kind == PreviewKind.MARKDOWN and self.markdown_source):
            ta = TextArea(
                result.text or "",
                read_only=True,
                show_line_numbers=True,
                soft_wrap=self.soft_wrap,
                id="preview-text",
            )
            await body.mount(ta)
            ta.focus()
            return

        if result.kind == PreviewKind.PDF:
            if result.pdf_visual_mode and result.pil_image is not None and result.image_engine_available:
                widget = self._build_image_widget(result)
                if widget is not None:
                    scroll = VerticalScroll(Center(widget, id="preview-image-center"), id="preview-scroll")
                    await body.mount(scroll)
                    scroll.focus()
                    return
            ta = TextArea(
                result.text or "",
                read_only=True,
                show_line_numbers=True,
                soft_wrap=self.soft_wrap,
                id="preview-text",
            )
            await body.mount(ta)
            ta.focus()
            return

        if result.kind == PreviewKind.TABLE:
            headers = result.table_headers or ["(empty)"]
            rows = result.table_rows or []
            table = DataTable(id="preview-table", zebra_stripes=True)
            for idx, header in enumerate(headers):
                table.add_column(Text(header), key=f"col-{idx}")
            for r_idx, row in enumerate(rows, start=1):
                padded = list(row[: len(headers)]) + [""] * max(0, len(headers) - len(row))
                table.add_row(*(Text(cell) for cell in padded), label=str(r_idx))
            await body.mount(table)
            table.focus()
            return

        if result.kind == PreviewKind.MARKDOWN:
            scroll = VerticalScroll(
                Markdown(result.text or "", open_links=False, id="preview-markdown"),
                id="preview-scroll",
            )
            await body.mount(scroll)
            scroll.focus()
            return

        if (
            result.kind == PreviewKind.IMAGE
            and result.pil_image is not None
            and result.image_engine_available
        ):
            widget = self._build_image_widget(result)
            if widget is not None:
                scroll = VerticalScroll(Center(widget, id="preview-image-center"), id="preview-scroll")
                await body.mount(scroll)
                scroll.focus()
                return

        # Metadata / Error / Optional image engine absent / Oversized image fallback
        meta_text = _format_metadata_lines(result.metadata, note=result.note, error=result.error)
        scroll = VerticalScroll(
            Static(meta_text, id="preview-meta", markup=False),
            id="preview-scroll",
        )
        await body.mount(scroll)
        scroll.focus()

    def _build_image_widget(self, result: PreviewResult):
        preview.ensure_safe_image_terminal()
        try:
            from textual_image.renderable.halfcell import Image as HalfcellRenderable
            from textual_image.widget import HalfcellImage, Image as AutoTerminalImage, UnicodeImage
        except Exception as exc:
            result.note = f"Image renderer unavailable: {sanitize_terminal_text(str(exc))}"
            return None

        mode = IMAGE_MODES[self.image_mode_index]
        cls = {"auto": AutoTerminalImage, "halfcell": HalfcellImage, "unicode": UnicodeImage}[mode]

        def on_error(exc: Exception) -> Static:
            return Static(
                _format_metadata_lines(
                    result.metadata,
                    note=f"Could not render image in {mode} mode: {sanitize_terminal_text(str(exc))}",
                ),
                id="preview-meta",
                markup=False,
            )

        def on_render_fallback(exc: Exception) -> None:
            msg = f"Switched to halfcell image fallback ({sanitize_terminal_text(str(exc))})"

            def _apply_notice() -> None:
                if not self.is_mounted:
                    return
                try:
                    notice = self.query_one("#preview-notice", Static)
                    notice.update(msg)
                    notice.display = True
                except Exception:
                    pass

            try:
                self.app.call_later(_apply_notice)
            except Exception:
                pass

        try:
            widget_cls = (
                _make_safe_image_widget_class(
                    cls,
                    fallback_renderable_cls=HalfcellRenderable,
                    on_render_fallback=on_render_fallback,
                )
                if isinstance(cls, type) and hasattr(cls, "_Renderable")
                else cls
            )
            return widget_cls(result.pil_image, id="preview-image", on_error=on_error)
        except Exception as exc:
            result.note = f"Image renderer unavailable: {sanitize_terminal_text(str(exc))}"
            return None

    # ---------------------------------------------------------------- actions
    def action_back(self) -> None:
        self._cancel.set()
        self._load_generation += 1
        self._stop_spinner()
        self.dismiss()

    def action_toggle_wrap(self) -> None:
        if self.result is None:
            return
        is_wrapped_text = (
            self.result.kind == PreviewKind.TEXT
            or (self.result.kind == PreviewKind.MARKDOWN and self.markdown_source)
            or (self.result.kind == PreviewKind.PDF and not self.result.pdf_visual_mode)
        )
        if is_wrapped_text:
            self.soft_wrap = not self.soft_wrap
            for ta in self.query("#preview-text"):
                if isinstance(ta, TextArea):
                    ta.soft_wrap = self.soft_wrap

    async def action_toggle_markdown_source(self) -> None:
        if self.result is None or self.result.kind != PreviewKind.MARKDOWN or self.markdown_too_large:
            return
        self.markdown_source = not self.markdown_source
        await self._render_result_safely()

    async def action_cycle_image_mode(self) -> None:
        if self.result is None:
            return
        is_visual_pdf = self.result.kind == PreviewKind.PDF and self.result.pdf_visual_mode
        if (
            self.result.kind not in (PreviewKind.IMAGE, PreviewKind.PDF)
            or (self.result.kind == PreviewKind.PDF and not is_visual_pdf)
            or self.result.pil_image is None
            or not self.result.image_engine_available
        ):
            return
        self.image_mode_index = (self.image_mode_index + 1) % len(IMAGE_MODES)
        settings.save_image_mode(IMAGE_MODES[self.image_mode_index])
        await self._render_result_safely()

    def action_toggle_pdf_visual(self) -> None:
        if self.result is None or self.result.kind != PreviewKind.PDF or self.result.page_count <= 0:
            return
        if not self.pdf_visual and not self.result.pdf_visual_available:
            notice = self.query_one("#preview-notice", Static)
            notice.update(
                f"Optional visual PDF renderer not installed ({preview.optional_engine_hint('pdf-render')})."
            )
            notice.display = True
            return
        self.pdf_visual = not self.pdf_visual
        self._start_load()

    def _step_page_or_sheet(self, delta: int) -> None:
        if self.result is None:
            return
        if self.result.kind == PreviewKind.PDF and self.result.page_count > 1:
            target = max(0, min(self.result.page_count - 1, self.page_index + delta))
            if target != self.page_index:
                self.page_index = target
                self._start_load()
            return
        if (
            self.result.kind == PreviewKind.TABLE
            and self.result.sheet_names
            and len(self.result.sheet_names) > 1
        ):
            target = max(0, min(len(self.result.sheet_names) - 1, self.sheet_index + delta))
            if target != self.sheet_index:
                self.sheet_index = target
                self._start_load()

    def action_prev_item(self) -> None:
        self._step_page_or_sheet(-1)

    def action_next_item(self) -> None:
        self._step_page_or_sheet(1)

    def action_choose_sheet(self) -> None:
        if (
            self.result is None
            or self.result.kind != PreviewKind.TABLE
            or not self.result.sheet_names
            or len(self.result.sheet_names) <= 1
        ):
            return
        names = list(self.result.sheet_names)

        def on_pick(selected: str | None) -> None:
            if selected is None or selected not in names:
                return
            idx = names.index(selected)
            if idx != self.sheet_index:
                self.sheet_index = idx
                self._start_load()

        self.app.push_screen(ChoiceScreen("Select worksheet:", names, initial=self.sheet_index), on_pick)

    def action_quick_look(self) -> None:
        """`o`: Quick Look on macOS, the desktop's default application on Linux."""
        if preview.is_macos():
            preview.toggle_preview(self.path)
            return
        error = preview.open_with_system_app(self.path)
        notice = self.query_one("#preview-notice", Static)
        notice.update(error or "Opened in the system's default application.")
        notice.display = True
