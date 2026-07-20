"""The Textual application shell."""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from jsm.config import settings as config
from jsm.core import AppContext
from jsm.database.models import JobStatus, QueueJob
from jsm.tui.browser import BrowserScreen
from jsm.tui.dashboard import DashboardScreen
from jsm.tui.logscreen import LogScreen
from jsm.tui.messages import JobUpdated
from jsm.tui.queue_view import QueueScreen

# Job states that mean "this job is not going to make further progress on its
# own right now" - used to decide when a batch of queued work has finished.
_SETTLED_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.WAITING_QUOTA,
    JobStatus.PAUSED,
}


class JsmApp(App):
    TITLE = "Synclayer"
    # No subtitle: keep the header compact so the media table gets the space.
    SUB_TITLE = ""
    CSS_PATH = "app.tcss"
    # The generic command palette is replaced by our own Menu (Ctrl+O), which
    # offers config/credentials/theme directly - so hide it from the footer.
    ENABLE_COMMAND_PALETTE = False

    MODES = {
        "dashboard": DashboardScreen,
        "browser": BrowserScreen,
        "queue": QueueScreen,
        "log": LogScreen,
    }

    BINDINGS = [
        Binding("1", "switch_mode('dashboard')", "Dashboard"),
        Binding("2", "switch_mode('browser')", "Browser"),
        Binding("3", "switch_mode('queue')", "Queue"),
        Binding("4", "switch_mode('log')", "Log"),
        Binding("ctrl+o", "menu", "Menu"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, ctx: AppContext | None = None):
        super().__init__()
        self.ctx = ctx or AppContext(on_job_update=self._job_updated)
        # ensure the callback reaches us even for an injected context
        self.ctx.worker.on_update = self._job_updated
        self.ctx_config_path = str(config.config_file())
        # Jobs seen active since the last time everything settled; when the
        # whole batch is done we surface a summary so no failure goes unseen.
        self._watched_jobs: dict[int, QueueJob] = {}

    def on_mount(self) -> None:
        self.theme = "dracula"
        self.switch_mode("browser")
        self.run_worker(self.ctx.worker.run_forever(), group="queue-worker",
                        description="download queue")

    # ------------------------------------------------------------------- menu

    def action_menu(self) -> None:
        from jsm.tui.options import OptionsMenu

        # Avoid stacking multiple menus (e.g. header click while open).
        if self.screen.__class__.__name__ == "OptionsMenu":
            return
        self.push_screen(OptionsMenu())

    def run_menu_action(self, key: str) -> None:
        from jsm.tui.options import FileEditScreen, ThemeScreen

        if key == "config":
            self.push_screen(FileEditScreen(
                config.config_file(), "Edit configuration", is_toml=True,
                help_text="Library paths, languages, api_key, tool paths. "
                          "Saved changes reload immediately.",
            ))
        elif key == "credentials":
            self.push_screen(FileEditScreen(
                config.accounts_file(), "Edit accounts / credentials",
                is_toml=False, private=True,
                help_text="One 'username;password' per line. Added on top of "
                          "the built-in default account (kept private, 0600).",
            ))
        elif key == "theme":
            self.push_screen(ThemeScreen())
        elif key == "log":
            self.switch_mode("log")
        elif key == "help":
            self.switch_mode("dashboard")
        elif key == "quit":
            self.run_worker(self.action_quit())

    def _job_updated(self, job: QueueJob) -> None:
        # Called by the queue worker inside the same event loop.
        self.post_message(JobUpdated(job))

    def on_job_updated(self, message: JobUpdated) -> None:
        if message.forwarded:
            return  # our own re-post bubbled back from a screen
        job = message.job
        if job.id is not None:
            self._watched_jobs[job.id] = job
            if all(j.status in _SETTLED_STATUSES
                   for j in self._watched_jobs.values()):
                batch = list(self._watched_jobs.values())
                self._watched_jobs.clear()
                self._batch_finished(batch)
        # Forward to whichever screen is active; screens refresh on resume.
        if self.screen_stack:
            self.screen.post_message(JobUpdated(message.job, forwarded=True))

    def _batch_finished(self, jobs: list[QueueJob]) -> None:
        """All queued work has settled: make the outcome unmissable."""
        from jsm.tui.dialogs import JobResultsDialog

        done = sum(1 for j in jobs if j.status == JobStatus.COMPLETED)
        bad = [j for j in jobs
               if j.status in (JobStatus.FAILED, JobStatus.WAITING_QUOTA)]
        if bad:
            # Failures get a modal the user must acknowledge - a toast that
            # fades after a few seconds is how errors were being missed.
            self.push_screen(JobResultsDialog(jobs))
        elif done:
            plural = "s" if done != 1 else ""
            self.notify(f"✓ {done} job{plural} completed", timeout=6)

    async def action_quit(self) -> None:
        self.ctx.worker.stop()
        await self.ctx.close()
        self.exit()


def run_tui() -> None:
    JsmApp().run()
