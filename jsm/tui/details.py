"""Details screen for a single media file."""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from jsm.database.models import MediaStatus, SyncStatus
from jsm.subtitles.language import language_name
from jsm.subtitles.matcher import guess_media

STATUS_LABEL = {
    MediaStatus.OK: "[green]✓ OK[/green]",
    MediaStatus.MISSING: "[red]✗ Missing[/red]",
    MediaStatus.WRONG_LANG: "[magenta]≠ Wrong language[/magenta]",
    MediaStatus.UNSYNCED: "[yellow]⚠ Unsynced[/yellow]",
}

SYNC_LABEL = {
    SyncStatus.SYNCED: "[green]synced[/green]",
    SyncStatus.UNSYNCED: "[yellow]unsynced[/yellow]",
    SyncStatus.SYNC_FAILED: "[red]sync failed[/red]",
    SyncStatus.UNKNOWN: "[dim]sync unknown[/dim]",
}


class DetailsScreen(ModalScreen):
    BINDINGS = [
        Binding("escape,v,q", "dismiss", "Close"),
    ]

    def __init__(self, media_id: int):
        super().__init__()
        self.media_id = media_id

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="details-panel", classes="dialog"):
            yield Static("", id="details-body")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#details-body", Static).update(self._render_details())

    def _render_details(self) -> str:
        ctx = self.app.ctx
        media = ctx.db.get_media(self.media_id)
        if media is None:
            return "File no longer in database."
        guess = guess_media(media.filename)
        lines = [
            f"[b]{escape(media.filename)}[/b]",
            "",
            f"  Path       {escape(media.path)}",
            f"  Status     {STATUS_LABEL.get(media.status, media.status)}",
            f"  Size       {media.size / 1e9:.2f} GB" if media.size > 1e9
            else f"  Size       {media.size / 1e6:.1f} MB",
        ]
        if media.duration:
            lines.append(f"  Duration   {int(media.duration // 60)} min")
        lines.append(f"  Hash       {media.hash or '[dim]not computed yet[/dim]'}")
        if media.scan_date:
            lines.append(f"  Scanned    {media.scan_date}")
        lines += [
            "",
            "[b]Parsed from filename[/b] (fallback matching)",
            f"  Title      {escape(guess.title) if guess.title else '-'}",
            f"  Year       {guess.year or '-'}",
            f"  Group      {escape(guess.release_group) if guess.release_group else '-'}",
            f"  Quality    {' '.join(filter(None, [guess.screen_size, guess.video_codec])) or '-'}",
            "",
            "[b]Subtitles[/b]",
        ]
        subs = ctx.db.subtitles_for(self.media_id)
        if not subs:
            lines.append("  [dim]none found[/dim]")
        for sub in subs:
            flags = []
            if sub.forced:
                flags.append("forced")
            if sub.hearing_impaired:
                flags.append("SDH")
            flag_text = f" [{', '.join(flags)}]" if flags else ""
            location = escape(Path(sub.path).name) if sub.path else "[dim]embedded stream[/dim]"
            lines.append(
                f"  {language_name(sub.language):<12} {sub.source:<10} "
                f"{SYNC_LABEL.get(SyncStatus(sub.sync_status), sub.sync_status)}"
                f"{flag_text}  {location}"
            )
            if sub.downloaded_date:
                lines.append(f"  {'':<12} [dim]downloaded {sub.downloaded_date}[/dim]")
        lines += ["", "[dim]Esc to close • M in browser for manual search[/dim]"]
        return "\n".join(lines)
