"""Dashboard: library health, account quotas, tool availability."""

from __future__ import annotations

import time

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from jsm.scanner.ffprobe import ffprobe_available
from jsm.subtitles.language import language_name
from jsm.subtitles.synchronizer import ffsubsync_available
from jsm.tui.header import MenuHeader
from jsm.tui.messages import JobUpdated


class DashboardScreen(Screen):
    BINDINGS = [Binding("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield MenuHeader()
        with VerticalScroll():
            with Grid(id="dashboard-grid"):
                yield Static("", id="dash-library", classes="dash-panel")
                yield Static("", id="dash-accounts", classes="dash-panel")
                yield Static("", id="dash-tools", classes="dash-panel")
                yield Static("", id="dash-help", classes="dash-panel")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_panels()

    def on_screen_resume(self) -> None:
        self.refresh_panels()

    def action_refresh(self) -> None:
        self.refresh_panels()

    def on_job_updated(self, event: JobUpdated) -> None:
        self.refresh_panels()

    def refresh_panels(self) -> None:
        ctx = self.app.ctx
        stats = ctx.db.media_stats()
        total = stats.get("total", 0)

        def bar(n: int) -> str:
            if not total:
                return ""
            width = 24
            filled = round(width * n / total)
            return "[dim]" + "█" * filled + "░" * (width - filled) + "[/dim]"

        library = [
            "[b]Library health[/b]",
            "",
            f"  Total media files   [b]{total}[/b]",
            f"  [green]✓ OK[/green]              {stats.get('ok', 0):>6}  {bar(stats.get('ok', 0))}",
            f"  [red]✗ Missing[/red]         {stats.get('missing', 0):>6}  {bar(stats.get('missing', 0))}",
            f"  [magenta]≠ Wrong lang[/magenta]      {stats.get('wrong_lang', 0):>6}  {bar(stats.get('wrong_lang', 0))}",
            f"  [yellow]⚠ Unsynced[/yellow]        {stats.get('unsynced', 0):>6}  {bar(stats.get('unsynced', 0))}",
            "",
            "  Wanted languages: "
            + ", ".join(language_name(l) for l in ctx.settings.languages),
            "  Libraries: " + (escape(", ".join(ctx.settings.libraries))
                               or "[yellow]none configured[/yellow]"),
        ]
        self.query_one("#dash-library", Static).update("\n".join(library))

        accounts = ["[b]OpenSubtitles accounts[/b]", ""]
        quotas = ctx.accounts.all_quotas()
        if not quotas:
            accounts.append("  [yellow]No accounts configured.[/yellow]")
            accounts.append("  Add username;password lines to accounts.conf")
        for quota in quotas:
            reset = ""
            if quota.next_reset and quota.remaining == 0:
                reset = time.strftime(" (resets %H:%M)", time.localtime(quota.next_reset))
            colour = "green" if quota.remaining > 5 else ("yellow" if quota.remaining else "red")
            accounts.append(
                f"  {escape(quota.username):<20} "
                f"[{colour}]{quota.remaining:>2}/20 left[/{colour}]{reset}"
            )
        self.query_one("#dash-accounts", Static).update("\n".join(accounts))

        def mark(ok: bool, label_ok: str, label_bad: str) -> str:
            return f"[green]✓[/green] {label_ok}" if ok else f"[red]✗[/red] {label_bad}"

        from jsm.subtitles.cleaner import subscleaner_available

        tools = [
            "[b]Tools & provider[/b]",
            "",
            "  " + mark(bool(ctx.accounts.usernames),
                       f"{len(ctx.accounts.usernames)} account(s) for login/rotation",
                       "no accounts in accounts.conf - downloads disabled"),
            "  " + mark(ffprobe_available(), "ffprobe available",
                       "ffprobe missing - no duration/embedded detection "
                       "(set ffprobe_path in config.toml if installed)"),
            "  " + mark(ffsubsync_available(), "ffsubsync available",
                       "ffsubsync missing - sync disabled "
                       "(set ffsubsync_path in config.toml if installed)"),
            "  " + mark(subscleaner_available(), "subscleaner available",
                       "subscleaner missing - cleanup disabled "
                       "(set subscleaner_path in config.toml if installed)"),
            "  " + mark(ctx.provider.has_api_key,
                       "API key available"
                       + (" (built-in)" if ctx.provider.uses_default_key
                          else " (config.toml)"),
                       "no API key - set api_key in config.toml "
                       "(free at opensubtitles.com/en/consumers)"),
        ]
        self.query_one("#dash-tools", Static).update("\n".join(tools))

        langs = ", ".join(ctx.settings.languages) or "en"
        help_text = [
            "[b]Keys[/b]",
            "",
            "  [b]1[/b] dashboard  [b]2[/b] browser  [b]3[/b] queue  [b]4[/b] activity log",
            "  [b]Ctrl+O[/b] menu (edit config / credentials / theme)  ·  [b]Ctrl+Q[/b] quit",
            "",
            "  [b]In the browser:[/b]",
            "  [b]Space[/b] tag a file   [b]D[/b] download+sync   [b]O[/b] download only",
            f"  [b]G[/b] both langs ({langs})   [b]S[/b] sync existing   [b]V[/b] details",
            "  [b]F[/b] show missing   [b]A[/b] show all   [b]H[/b] hide files that are done",
            "  [b]L/U[/b] show wrong-lang / unsynced   [b]M[/b] manual search",
            "  [b]B[/b] bulk download (typed confirmation)   [b]R[/b] rescan folder",
            "",
            "  [dim]Queued work runs in the background - the status bar shows",
            "  running/failed counts, a summary pops up when a batch ends, and",
            "  the activity log ([b]4[/b]) keeps the full history incl. sync results.[/dim]",
            "",
            f"  [dim]default per download: clean + sync · primary language {ctx.settings.primary_language}[/dim]",
        ]
        self.query_one("#dash-help", Static).update("\n".join(help_text))
