from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from .diff import compare
from .modals import MessageScreen
from .models import DiffStatus, OpKind, OpSide, OperationQueue, QueuedOp
from .plan import plan_mirror, plan_two_way, plan_update
from .run_queue import run_queue_with_progress

STATUS_LABEL = {
    DiffStatus.LEFT_ONLY: "< only left",
    DiffStatus.RIGHT_ONLY: "> only right",
    DiffStatus.MODIFIED: "! modified",
    DiffStatus.IDENTICAL: "= identical",
}


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

    def compose(self) -> ComposeResult:
        yield Static("", id="compare-summary")
        yield DataTable(id="compare-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        self.diffs = compare(self.left_root, self.right_root)
        table = self.query_one("#compare-table", DataTable)
        table.clear(columns=True)
        table.add_columns(" ", "Path", "Status")
        for d in self.diffs:
            if d.status == DiffStatus.IDENTICAL:
                continue
            mark = "*" if d.rel_path in self.manual_marks else ""
            table.add_row(mark, d.rel_path, STATUS_LABEL[d.status], key=d.rel_path)

        non_identical = sum(1 for d in self.diffs if d.status != DiffStatus.IDENTICAL)
        summary = self.query_one("#compare-summary", Static)
        summary.update(
            f"{self.left_root}  <->  {self.right_root}\n"
            f"{non_identical} differences   "
            "[m]irror  [u]pdate  [b]idirectional  [space] mark  [c]opy marked  [esc] back"
        )

    def action_back(self) -> None:
        self.dismiss()

    def _run_queue(self, title: str, queue: OperationQueue) -> None:
        run_queue_with_progress(
            self, title, queue, self.left_root, self.right_root, after=lambda report: self.reload()
        )

    def action_mirror(self) -> None:
        active = [d for d in self.diffs if d.status != DiffStatus.IDENTICAL]
        self._run_queue("Mirror: make RIGHT identical to LEFT", plan_mirror(active))

    def action_update(self) -> None:
        active = [d for d in self.diffs if d.status != DiffStatus.IDENTICAL]
        self._run_queue("Update: copy new/changed LEFT -> RIGHT (no deletes)", plan_update(active))

    def action_two_way(self) -> None:
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
        self.reload()

    def action_copy_manual(self) -> None:
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
                queue.add(QueuedOp(OpKind.COPY, OpSide.LEFT_TO_RIGHT, rel_path, bool(d.left and d.left.kind.value == "dir"), size=d.left.size if d.left else 0))
            elif d.status == DiffStatus.RIGHT_ONLY:
                queue.add(QueuedOp(OpKind.COPY, OpSide.RIGHT_TO_LEFT, rel_path, bool(d.right and d.right.kind.value == "dir"), size=d.right.size if d.right else 0))
        self.manual_marks.clear()
        self._run_queue("Copy marked items", queue)
