"""Activity Log screen: a live, scrollable history of everything the app does.

This is the answer to "did it actually sync, or was it already fine?" - every
search, download, clean and sync outcome lands here with a timestamp, and new
lines stream in live while the screen is open.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from jsm.activity import ERROR, INFO, OK, WARN, LogEntry
from jsm.tui.header import MenuHeader
from jsm.tui.messages import ActivityLogged

_LEVEL_STYLE = {
    INFO: "dim",
    OK: "green",
    WARN: "yellow",
    ERROR: "bold red",
}
_LEVEL_ICON = {INFO: "·", OK: "✓", WARN: "!", ERROR: "✗"}


class LogScreen(Screen):
    BINDINGS = [
        Binding("c", "clear", "Clear view"),
        Binding("g", "scroll_home", "Top", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield MenuHeader()
        yield Static("", id="log-status")
        yield RichLog(id="activity-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._render_all()
        # Stream new entries in while this screen is open.
        self.app.ctx.activity.subscribe(self._on_entry)

    def on_unmount(self) -> None:
        self.app.ctx.activity.unsubscribe(self._on_entry)

    def on_screen_resume(self) -> None:
        self._render_all()

    def _on_entry(self, entry: LogEntry) -> None:
        # Called from whatever thread logged; posting a message is thread-safe.
        self.post_message(ActivityLogged(entry))

    def on_activity_logged(self, message: ActivityLogged) -> None:
        self._write(message.entry)
        self._update_status()

    def _render_all(self) -> None:
        log = self.query_one("#activity-log", RichLog)
        log.clear()
        for entry in self.app.ctx.activity.entries():
            self._write(entry)
        self._update_status()

    def _write(self, entry: LogEntry) -> None:
        log = self.query_one("#activity-log", RichLog)
        style = _LEVEL_STYLE.get(entry.level, "")
        icon = _LEVEL_ICON.get(entry.level, "·")
        line = Text()
        line.append(f"{entry.clock} ", style="dim")
        line.append(f"{icon} ", style=style)
        line.append(entry.message, style=style)
        log.write(line)

    def _update_status(self) -> None:
        n = len(tuple(self.app.ctx.activity.entries()))
        self.query_one("#log-status", Static).update(
            f"[b]Activity log[/b]  •  {n} event(s)  •  newest at the bottom  "
            "•  [dim]C to clear the view[/dim]"
        )

    def action_clear(self) -> None:
        # Clears the on-screen view only; the underlying history is kept.
        self.query_one("#activity-log", RichLog).clear()

    def action_scroll_home(self) -> None:
        self.query_one("#activity-log", RichLog).scroll_home()

    def action_scroll_end(self) -> None:
        self.query_one("#activity-log", RichLog).scroll_end()
