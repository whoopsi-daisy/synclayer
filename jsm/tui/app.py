"""The Textual application shell."""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from jsm.config import settings as config
from jsm.core import AppContext
from jsm.database.models import QueueJob
from jsm.tui.browser import BrowserScreen
from jsm.tui.dashboard import DashboardScreen
from jsm.tui.messages import JobUpdated
from jsm.tui.queue_view import QueueScreen


class JsmApp(App):
    TITLE = "Synclayer"
    SUB_TITLE = "Jellyfin Subtitle Maintenance Manager"
    CSS_PATH = "app.tcss"

    MODES = {
        "dashboard": DashboardScreen,
        "browser": BrowserScreen,
        "queue": QueueScreen,
    }

    BINDINGS = [
        Binding("1", "switch_mode('dashboard')", "Dashboard"),
        Binding("2", "switch_mode('browser')", "Browser"),
        Binding("3", "switch_mode('queue')", "Queue"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, ctx: AppContext | None = None):
        super().__init__()
        self.ctx = ctx or AppContext(on_job_update=self._job_updated)
        # ensure the callback reaches us even for an injected context
        self.ctx.worker.on_update = self._job_updated
        self.ctx_config_path = str(config.config_file())

    def on_mount(self) -> None:
        self.switch_mode("browser")
        self.run_worker(self.ctx.worker.run_forever(), group="queue-worker",
                        description="download queue")

    def _job_updated(self, job: QueueJob) -> None:
        # Called by the queue worker inside the same event loop.
        self.post_message(JobUpdated(job))

    def on_job_updated(self, message: JobUpdated) -> None:
        # Forward to whichever screen is active; screens refresh on resume.
        if self.screen_stack:
            self.screen.post_message(JobUpdated(message.job))

    async def action_quit(self) -> None:
        self.ctx.worker.stop()
        await self.ctx.close()
        self.exit()


def run_tui() -> None:
    JsmApp().run()
