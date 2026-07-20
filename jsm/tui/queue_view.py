"""Queue screen: live job list with pause/resume/retry/priority controls."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from jsm.database.models import ACTIVE_JOB_STATUSES, JobStatus, QueueJob
from jsm.tui.header import MenuHeader
from jsm.tui.messages import JobUpdated

STATUS_STYLE = {
    JobStatus.QUEUED: "cyan",
    JobStatus.SEARCHING: "yellow",
    JobStatus.DOWNLOADING: "yellow",
    JobStatus.SYNCING: "yellow",
    JobStatus.COMPLETED: "green",
    JobStatus.FAILED: "red",
    JobStatus.PAUSED: "dim",
    JobStatus.WAITING_QUOTA: "magenta",
}


class QueueScreen(Screen):
    BINDINGS = [
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("t", "retry", "Retry"),
        Binding("plus_sign,plus", "prioritize(1)", "Prio +", show=False),
        Binding("minus,hyphen", "prioritize(-1)", "Prio -", show=False),
        Binding("c", "clear_finished", "Clear done"),
    ]

    def compose(self) -> ComposeResult:
        yield MenuHeader()
        yield Static("", id="queue-status")
        yield DataTable(id="queue-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        self._columns = table.add_columns(
            "#", "File", "Action", "Lang", "Status", "Prio", "Info"
        )
        self.refresh_table()

    def on_screen_resume(self) -> None:
        self.refresh_table()

    def refresh_table(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        jobs = self.app.ctx.db.jobs()
        for job in jobs:
            table.add_row(*self._row_cells(job), key=str(job.id))
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        active = sum(1 for j in jobs if j.status in ACTIVE_JOB_STATUSES)
        self.query_one("#queue-status", Static).update(
            f"[b]Queue[/b]  •  {len(jobs)} job(s), {active} active"
        )

    @staticmethod
    def _row_cells(job: QueueJob) -> list:
        info = job.error_message if job.status == JobStatus.FAILED else (job.detail or "")
        return [
            str(job.id),
            # Text() renders literally: filenames and error messages often
            # contain brackets ('[Group] Movie', '[Errno 2] ...') that must
            # not be parsed as Rich markup.
            Text(Path(job.media_path or "?").name),
            job.action.replace("_", "+"),
            job.language,
            Text(job.status, style=STATUS_STYLE.get(JobStatus(job.status), "")),
            str(job.priority),
            Text(info or ""),
        ]

    def _cursor_job_id(self) -> int | None:
        table = self.query_one("#queue-table", DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        return int(key.value) if key.value else None

    def action_pause(self) -> None:
        job_id = self._cursor_job_id()
        if job_id is not None:
            self.app.ctx.worker.pause(job_id)
            self.refresh_table()

    def action_resume(self) -> None:
        job_id = self._cursor_job_id()
        if job_id is not None:
            self.app.ctx.worker.resume(job_id)
            self.refresh_table()

    def action_retry(self) -> None:
        job_id = self._cursor_job_id()
        if job_id is not None:
            self.app.ctx.worker.retry(job_id)
            self.refresh_table()

    def action_prioritize(self, delta: int) -> None:
        job_id = self._cursor_job_id()
        if job_id is None:
            return
        job = self.app.ctx.db.get_job(job_id)
        if job is not None:
            self.app.ctx.worker.reprioritize(job_id, job.priority + delta)
            self.refresh_table()

    def action_clear_finished(self) -> None:
        removed = self.app.ctx.db.clear_finished_jobs()
        self.notify(f"Removed {removed} finished job(s)")
        self.refresh_table()

    def on_job_updated(self, event: JobUpdated) -> None:
        self.refresh_table()
