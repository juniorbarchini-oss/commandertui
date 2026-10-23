from __future__ import annotations

import threading
import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Static

from .diff import compare
from .modals import MessageScreen
from .models import DiffEntry, DiffStatus, EntryKind, FileEntry, OpKind, OpSide, OperationQueue, QueuedOp
from .plan import plan_mirror, plan_two_way, plan_update
from .run_queue import run_queue_with_progress
from .widgets import SPINNER_FRAMES, CompareBar, _format_bytes

STATUS_LABEL = {
    DiffStatus.LEFT_ONLY: "< only left",
    DiffStatus.RIGHT_ONLY: "> only right",
    DiffStatus.MODIFIED: "! modified",
    DiffStatus.IDENTICAL: "= identical",
}


def side_info(entry: FileEntry | None) -> str:
    """"1.2 MB  2026-10-20 14:03" for one side of a difference ("-" if absent)."""
    if entry is None:
        return "-"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))
    size = "<dir>" if entry.kind == EntryKind.DIR else _format_bytes(entry.size)
    return f"{size}  {when}"


def status_text(d: DiffEntry) -> str:
    """Status label; for a changed file also says which side has the newer copy."""
    label = STATUS_LABEL[d.status]
    if d.status == DiffStatus.MODIFIED and d.left and d.right and d.left.mtime != d.right.mtime:
        label += " (left newer)" if d.left.mtime > d.right.mtime else " (right newer)"
    return label


class CompareScreen(Screen):
    """Full recursive diff between the two panes' current directories, with
    bulk actions (mirror/update/two-way) or per-item manual handling.
    """

    # priority=True: same reason as CommanderApp -- the focused DataTable
    # would otherwise intercept space/enter-like keys before we see them.
    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("m", "mirror", "Mirror ->", priority=True),
        Binding("u", "update", "Update ->", priority=True),
        Binding("b", "two_way", "Two-way", priority=True),
        Binding("space", "toggle_manual", "Mark", priority=True),
        Binding("c", "copy_manual", "Copy marked ->", priority=True),
    ]

    def __init__(self, left_root: str, right_root: str) -> None:
        super().__init__()
        self.left_root = left_root
        self.right_root = right_root
        self.manual_marks: set[str] = set()
        self.diffs: list[DiffEntry] = []
        self.scanning = False
        self._cancel = threading.Event()
        self._spinner = None

    def compose(self) -> ComposeResult:
        yield Static("", id="compare-summary")
        yield DataTable(id="compare-table", cursor_type="row")
        yield CompareBar()

    def on_mount(self) -> None:
        self.reload()

    def _stop_spinner(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    def reload(self) -> None:
        """Compare in a worker thread (a big tree can take minutes), with a
        spinner so the screen never looks frozen; Esc cancels."""
        if self.scanning:
            return
        self.scanning = True
        cancel = self._cancel = threading.Event()
        summary = self.query_one("#compare-summary", Static)
        started = time.monotonic()
        frame = 0

        def spin() -> None:
            nonlocal frame
            frame += 1
            summary.update(
                f"{self.left_root}  <->  {self.right_root}\n"
                f"Comparing {SPINNER_FRAMES[frame % 4]}  {int(time.monotonic() - started)}s"
                "   (Esc to cancel)"
            )

        spin()
        self._spinner = self.set_interval(0.12, spin)

        def work() -> None:
            try:
                diffs, error = compare(self.left_root, self.right_root, cancel_check=cancel.is_set), ""
            except OSError as e:
                diffs, error = [], str(e)
            if not cancel.is_set():
                self.app.call_from_thread(self._scan_done, diffs, error)

        self.run_worker(work, thread=True)

    def _scan_done(self, diffs: list[DiffEntry], error: str) -> None:
        self._stop_spinner()
        self.scanning = False
        if error:
            self.app.push_screen(MessageScreen(f"Could not compare:\n{error}"))
        self.diffs = diffs
        self._fill_table()

    def _fill_table(self) -> None:
        table = self.query_one("#compare-table", DataTable)
        table.clear(columns=True)
        table.add_columns(" ", "Path", "Status", "Left (size, date)", "Right (size, date)")
        for d in self.diffs:
            if d.status == DiffStatus.IDENTICAL:
                continue
            mark = "*" if d.rel_path in self.manual_marks else ""
            table.add_row(
                mark, d.rel_path, status_text(d), side_info(d.left), side_info(d.right), key=d.rel_path
            )

        non_identical = sum(1 for d in self.diffs if d.status != DiffStatus.IDENTICAL)
        summary = self.query_one("#compare-summary", Static)
        summary.update(
            f"{self.left_root}  <->  {self.right_root}\n"
            f"{non_identical} differences"
        )

    def action_back(self) -> None:
        self._cancel.set()  # stops a running comparison; its late result is dropped
        self._stop_spinner()
        self.dismiss()

    def _busy(self) -> bool:
        if self.scanning:
            self.notify("Still comparing. Wait for it to finish, or press Esc to cancel.", severity="warning")
        return self.scanning

    def _run_queue(self, title: str, queue: OperationQueue) -> None:
        run_queue_with_progress(
            self, title, queue, self.left_root, self.right_root, after=lambda report: self.reload()
        )

    def action_mirror(self) -> None:
        if self._busy():
            return
        active = [d for d in self.diffs if d.status != DiffStatus.IDENTICAL]
        self._run_queue("Mirror: make RIGHT identical to LEFT", plan_mirror(active))

    def action_update(self) -> None:
        if self._busy():
            return
        active = [d for d in self.diffs if d.status != DiffStatus.IDENTICAL]
        self._run_queue("Update: copy new/changed LEFT -> RIGHT (no deletes)", plan_update(active))

    def action_two_way(self) -> None:
        if self._busy():
            return
        active = [d for d in self.diffs if d.status != DiffStatus.IDENTICAL]
        queue, conflicts = plan_two_way(active)
        if conflicts:
            names = "\n".join(f"  {c.rel_path}" for c in conflicts[:10])
            self.app.push_screen(
                MessageScreen(
                    f"{len(conflicts)} conflict(s) changed on both sides -- "
                    f"resolve manually with [space]+[c]:\n{names}"
                )
            )
        self._run_queue("Two-way: propagate each side's exclusive changes", queue)

    def action_toggle_manual(self) -> None:
        if self._busy():
            return
        table = self.query_one("#compare-table", DataTable)
        if table.row_count == 0:
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        if row_key is None or row_key.value is None:
            return
        rel_path = str(row_key.value)
        if rel_path in self.manual_marks:
            self.manual_marks.discard(rel_path)
        else:
            self.manual_marks.add(rel_path)
        self._fill_table()

    def action_copy_manual(self) -> None:
        if self._busy():
            return
        if not self.manual_marks:
            self.app.push_screen(MessageScreen("No items marked. Use [space] first."))
            return
        by_path = {d.rel_path: d for d in self.diffs}
        queue = OperationQueue()
        for rel_path in self.manual_marks:
            d = by_path.get(rel_path)
            if d is None:
                continue
            if d.status == DiffStatus.LEFT_ONLY or (d.status == DiffStatus.MODIFIED and d.left):
                queue.add(
                    QueuedOp(
                        OpKind.COPY,
                        OpSide.LEFT_TO_RIGHT,
                        rel_path,
                        bool(d.left and d.left.kind.value == "dir"),
                        size=d.left.size if d.left else 0,
                    )
                )
            elif d.status == DiffStatus.RIGHT_ONLY:
                queue.add(
                    QueuedOp(
                        OpKind.COPY,
                        OpSide.RIGHT_TO_LEFT,
                        rel_path,
                        bool(d.right and d.right.kind.value == "dir"),
                        size=d.right.size if d.right else 0,
                    )
                )
        self.manual_marks.clear()
        self._run_queue("Copy marked items", queue)
