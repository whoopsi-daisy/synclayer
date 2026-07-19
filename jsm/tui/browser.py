"""Interactive library browser: tree on the left, media table on the right.

Keys: Space select, F/L/U/A filters, D download+sync, O download only,
S sync, M manual search, V details, R rescan folder, B bulk download.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

from jsm.database.models import JobAction, Media, MediaStatus
from jsm.subtitles.language import language_name
from jsm.tui.dialogs import BulkChoice, BulkConfirmDialog, ManualSearch, ManualSearchDialog
from jsm.tui.messages import JobUpdated, ScanFinished

STATUS_DISPLAY: dict[str, Text] = {
    MediaStatus.OK: Text("✓ OK", style="bold green"),
    MediaStatus.MISSING: Text("✗ Missing", style="bold red"),
    MediaStatus.WRONG_LANG: Text("≠ Wrong lang", style="bold magenta"),
    MediaStatus.UNSYNCED: Text("⚠ Unsynced", style="bold yellow"),
}

FILTERS: dict[str, str | None] = {
    "all": None,
    "missing": MediaStatus.MISSING,
    "wrong language": MediaStatus.WRONG_LANG,
    "unsynced": MediaStatus.UNSYNCED,
}


class LibraryTree(Tree[str]):
    """Lazy directory tree over the configured library roots."""

    def __init__(self, roots: list[Path], **kwargs):
        super().__init__("Libraries", **kwargs)
        self.show_root = False
        self.guide_depth = 2
        for root in roots:
            node = self.root.add(str(root), data=str(root))
            node.allow_expand = True

    @staticmethod
    def _subdirs(path: str) -> list[Path]:
        try:
            return sorted(
                p for p in Path(path).iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        except OSError:
            return []

    def _populate(self, node: TreeNode) -> None:
        if node.data is None or node.children:
            return
        for sub in self._subdirs(node.data):
            child = node.add(sub.name, data=str(sub))
            child.allow_expand = bool(self._subdirs(str(sub)))

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        self._populate(event.node)


class BrowserScreen(Screen):
    BINDINGS = [
        Binding("space", "toggle_select", "Select"),
        Binding("ctrl+a", "select_all", "Select all"),
        Binding("escape", "clear_selection", "Clear sel", show=False),
        Binding("f", "filter('missing')", "Missing"),
        Binding("l", "filter('wrong language')", "Wrong lang"),
        Binding("u", "filter('unsynced')", "Unsynced"),
        Binding("a", "filter('all')", "All"),
        Binding("h", "toggle_hide_ok", "Hide done"),
        Binding("d", "download_sync", "Download"),
        Binding("g", "download_all_langs", "Get both langs"),
        Binding("o", "download_only", "Download (no sync)"),
        Binding("s", "sync", "Sync"),
        Binding("m", "manual_search", "Manual", show=False),
        Binding("v", "details", "Details"),
        Binding("r", "rescan", "Rescan"),
        Binding("b", "bulk", "Bulk DL"),
    ]

    def __init__(self):
        super().__init__()
        self.current_dir: str | None = None
        self.filter_name = "all"
        # Declutter toggle: drop rows that already have a healthy subtitle.
        self.hide_ok = False
        self.selected: set[int] = set()
        self._media_cache: dict[int, Media] = {}

    # --------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="browser-left"):
                yield LibraryTree(self.app.ctx.settings.library_paths, id="library-tree")
            with Vertical(id="browser-right"):
                yield Static("", id="browser-status")
                yield DataTable(id="media-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#media-table", DataTable)
        self._columns = table.add_columns("", "Title", "Status", "Subtitles", "Size")
        roots = self.app.ctx.settings.library_paths
        if not roots:
            self.query_one("#browser-status", Static).update(
                "[yellow]No libraries configured.[/] Add paths to 'libraries' in "
                f"[b]{self.app.ctx_config_path}[/b] and restart."
            )
        else:
            self.open_directory(str(roots[0]))

    def on_screen_resume(self) -> None:
        self.refresh_table()

    # ------------------------------------------------------------ tree events

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data:
            self.open_directory(event.node.data)

    # ------------------------------------------------------------- table data

    def open_directory(self, directory: str) -> None:
        self.current_dir = directory
        self.selected.clear()
        self.refresh_table()
        self.query_one("#media-table", DataTable).focus()
        self._scan_directory(directory)

    def _visible_media(self) -> list[Media]:
        if self.current_dir is None:
            return []
        media = self.app.ctx.db.media_in_directory(self.current_dir)
        status = FILTERS[self.filter_name]
        if status:
            media = [m for m in media if m.status == status]
        if self.hide_ok:
            media = [m for m in media if m.status != MediaStatus.OK]
        return media

    def _subtitle_summary(self, media: Media, subs=None) -> str:
        if subs is None:
            subs = self.app.ctx.db.subtitles_for(media.id)
        if not subs:
            return "-"
        parts = []
        for sub in subs:
            label = sub.language
            if sub.source == "embedded":
                label += "*"
            if sub.forced:
                label += "!"
            parts.append(label)
        return ",".join(dict.fromkeys(parts))

    @staticmethod
    def _human_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{value:.1f} TB"

    def refresh_table(self) -> None:
        table = self.query_one("#media-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        self._media_cache.clear()
        visible = self._visible_media()
        subs_map = self.app.ctx.db.subtitles_by_media(
            [m.id for m in visible if m.id is not None]
        )
        for media in visible:
            assert media.id is not None
            self._media_cache[media.id] = media
            table.add_row(
                "●" if media.id in self.selected else "",
                # Text() renders literally - filenames like "[Group] Movie.mkv"
                # must not be parsed as Rich markup.
                Text(media.filename),
                STATUS_DISPLAY.get(media.status, Text(media.status)),
                Text(self._subtitle_summary(media, subs_map.get(media.id))),
                self._human_size(media.size),
                key=str(media.id),
            )
        # A filter change can hide selected rows; drop them so the shown
        # selection count always matches what actions will operate on.
        self.selected &= set(self._media_cache)
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        self._update_status_bar()

    def _update_status_bar(self, extra: str = "") -> None:
        if self.current_dir is None:
            return
        n = len(self._media_cache)
        parts = [
            f"[b]{escape(self.current_dir)}[/b]",
            f"{n} file(s)",
            f"filter: [b]{self.filter_name}[/b]"
            + (" [dim](hiding ✓ OK - press H to show)[/dim]" if self.hide_ok else ""),
        ]
        if self.selected:
            parts.append(f"[reverse] {len(self.selected)} selected [/reverse]")
        parts.extend(self._job_summary())
        if extra:
            parts.append(extra)
        self.query_one("#browser-status", Static).update("  •  ".join(parts))

    def _job_summary(self) -> list[str]:
        """Live queue counters so running/failed work is visible from the
        browser without switching to the queue screen."""
        from jsm.database.models import ACTIVE_JOB_STATUSES, JobStatus

        jobs = self.app.ctx.db.jobs()
        active = sum(1 for j in jobs if j.status in ACTIVE_JOB_STATUSES)
        failed = sum(1 for j in jobs if j.status == JobStatus.FAILED)
        parts = []
        if active:
            parts.append(f"[yellow]⏳ {active} job(s) running[/yellow]")
        if failed:
            parts.append(f"[red]✗ {failed} failed - press 3 for details[/red]")
        return parts

    # -------------------------------------------------------------- scanning

    @work(thread=True, exclusive=True, group="scan")
    def _scan_directory(self, directory: str) -> None:
        from jsm.scanner.filesystem import ScanStats

        try:
            db, scanner = self.app.ctx.new_scanner()
            try:
                stats = scanner.scan(directory, recursive=False)
            finally:
                db.close()
        except Exception as exc:  # a failed scan must not kill the worker
            stats = ScanStats(warnings=[f"Scan failed: {exc}"])
        self.app.call_from_thread(
            self.post_message, ScanFinished(directory, stats)
        )

    def on_scan_finished(self, event: ScanFinished) -> None:
        if event.directory == self.current_dir:
            self.refresh_table()
        for warning in event.stats.warnings:
            self.notify(escape(warning), severity="warning", timeout=6)

    def action_rescan(self) -> None:
        if self.current_dir:
            self._update_status_bar("[i]scanning…[/i]")
            self._scan_directory(self.current_dir)

    # -------------------------------------------------------------- selection

    def _cursor_media(self) -> Media | None:
        table = self.query_one("#media-table", DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        return self._media_cache.get(int(key.value)) if key.value else None

    def _target_media(self) -> list[Media]:
        """Selected files, or the cursor row when nothing is selected."""
        if self.selected:
            return [m for mid, m in self._media_cache.items() if mid in self.selected]
        media = self._cursor_media()
        return [media] if media else []

    def action_toggle_select(self) -> None:
        media = self._cursor_media()
        if media is None or media.id is None:
            return
        table = self.query_one("#media-table", DataTable)
        if media.id in self.selected:
            self.selected.discard(media.id)
        else:
            self.selected.add(media.id)
        table.update_cell(
            str(media.id), self._columns[0],
            "●" if media.id in self.selected else "",
        )
        if table.cursor_row < table.row_count - 1:
            table.move_cursor(row=table.cursor_row + 1)
        self._update_status_bar()

    def action_select_all(self) -> None:
        self.selected = set(self._media_cache)
        self.refresh_table()

    def action_clear_selection(self) -> None:
        self.selected.clear()
        self.refresh_table()

    def action_filter(self, name: str) -> None:
        self.filter_name = name
        self.refresh_table()

    def action_toggle_hide_ok(self) -> None:
        self.hide_ok = not self.hide_ok
        self.refresh_table()
        if self.hide_ok:
            self.notify("Hiding files that already have subtitles (H to undo)",
                        timeout=4)

    # ---------------------------------------------------------------- actions

    @property
    def _language(self) -> str:
        return self.app.ctx.settings.primary_language

    @property
    def _all_languages(self) -> list[str]:
        langs = self.app.ctx.settings.languages
        return list(langs) if langs else ["en"]

    def _enqueue(self, action: str, targets: list[Media],
                 languages: list[str] | None = None) -> None:
        if not targets:
            self.notify("Nothing selected", severity="warning")
            return
        langs = languages or [self._language]
        for media in targets:
            assert media.id is not None
            for language in langs:
                self.app.ctx.worker.enqueue(media.id, action, language)
        verb = {
            JobAction.DOWNLOAD: "download",
            JobAction.DOWNLOAD_SYNC: "download",
            JobAction.SYNC: "sync",
        }[JobAction(action)]
        lang_note = f" [{', '.join(langs)}]" if len(langs) > 1 else ""
        self.notify(
            f"▶ Queued {verb}{lang_note} for {len(targets)} file(s) - "
            "running in the background; a summary appears when done",
            timeout=5,
        )
        self.selected.clear()
        self.refresh_table()

    def action_download_sync(self) -> None:
        self._enqueue(JobAction.DOWNLOAD_SYNC, self._target_media())

    def action_download_all_langs(self) -> None:
        self._enqueue(JobAction.DOWNLOAD_SYNC, self._target_media(),
                      languages=self._all_languages)

    def action_download_only(self) -> None:
        self._enqueue(JobAction.DOWNLOAD, self._target_media())

    def action_sync(self) -> None:
        self._enqueue(JobAction.SYNC, self._target_media())

    def action_details(self) -> None:
        media = self._cursor_media()
        if media is not None:
            from jsm.tui.details import DetailsScreen

            self.app.push_screen(DetailsScreen(media.id))

    # ----------------------------------------------------------- manual search

    def action_manual_search(self) -> None:
        media = self._cursor_media()
        if media is None:
            return
        from jsm.subtitles.matcher import guess_media

        guess = guess_media(media.filename)

        def handle(result: ManualSearch | None) -> None:
            if result is not None:
                self._manual_download(media, result)

        self.app.push_screen(
            ManualSearchDialog(
                media.filename,
                guess.title or Path(media.path).stem,
                guess.year,
                self._language,
            ),
            handle,
        )

    @work(exclusive=False, group="manual")
    async def _manual_download(self, media: Media, search: ManualSearch) -> None:
        self.notify(f"Manual search: {escape(search.query)}…")
        try:
            outcome = await self.app.ctx.downloader.download_for(
                media, search.language, query=search.query, year=search.year
            )
        except Exception as exc:
            self.notify(f"Manual search failed: {escape(str(exc))}",
                        severity="error", timeout=8)
            return
        self.notify(
            escape(outcome.message),
            severity="information" if outcome.success else "warning",
            timeout=8,
        )
        self.refresh_table()

    # ------------------------------------------------------------------- bulk

    def action_bulk(self) -> None:
        if self.current_dir is None:
            return
        targets = self.app.ctx.db.media_under(self.current_dir, status=MediaStatus.MISSING)
        if not targets:
            self.notify("No files with missing subtitles under this folder")
            return

        def handle(choice: BulkChoice | None) -> None:
            if choice is not None:
                self._run_bulk(targets, choice)

        self.app.push_screen(
            BulkConfirmDialog(
                len(targets),
                self.app.ctx.settings.bulk_min_confidence,
                self.app.ctx.settings.sync_by_default,
            ),
            handle,
        )

    def _run_bulk(self, targets: list[Media], choice: BulkChoice) -> None:
        if choice.dry_run:
            self._bulk_dry_run(targets, choice)
            return
        action = JobAction.DOWNLOAD_SYNC if choice.sync else JobAction.DOWNLOAD
        for media in targets:
            assert media.id is not None
            self.app.ctx.worker.enqueue(
                media.id, action, self._language, min_confidence=choice.min_confidence
            )
        self.notify(f"Queued {len(targets)} bulk download(s)")

    @work(exclusive=True, group="bulk-dry")
    async def _bulk_dry_run(self, targets: list[Media], choice: BulkChoice) -> None:
        self.notify(f"Dry run over {len(targets)} file(s)…")
        would = 0
        for media in targets:
            try:
                outcome = await self.app.ctx.downloader.download_for(
                    media, self._language,
                    min_confidence=choice.min_confidence, dry_run=True,
                )
            except Exception as exc:
                self.notify(f"Dry run stopped: {exc}", severity="error", timeout=8)
                return
            if outcome.success:
                would += 1
        self.notify(
            f"Dry run: {would}/{len(targets)} file(s) would get a subtitle "
            f"at ≥{choice.min_confidence:.0%} confidence",
            timeout=10,
        )

    # ------------------------------------------------------------ job updates

    def on_job_updated(self, event: JobUpdated) -> None:
        self._update_status_bar()
        media_id = event.job.media_id
        if media_id in self._media_cache:
            refreshed = self.app.ctx.db.get_media(media_id)
            if refreshed is None:
                return
            self._media_cache[media_id] = refreshed
            table = self.query_one("#media-table", DataTable)
            try:
                table.update_cell(
                    str(media_id), self._columns[2],
                    STATUS_DISPLAY.get(refreshed.status, Text(refreshed.status)),
                )
                table.update_cell(
                    str(media_id), self._columns[3],
                    Text(self._subtitle_summary(refreshed)),
                )
            except Exception:
                self.refresh_table()
