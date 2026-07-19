"""Textual Pilot tests: drive the real app headless against a fake provider."""

import pytest
from textual.widgets import DataTable, Input

from jsm.config.settings import config_file, ensure_first_run_files
from jsm.core import AppContext
from jsm.database.models import JobStatus, MediaStatus
from jsm.subtitles.downloader import Downloader
from jsm.tui.app import JsmApp
from jsm.tui.browser import BrowserScreen
from jsm.tui.dialogs import BulkConfirmDialog
from tests.conftest import SRT, FakeProvider


@pytest.fixture
def app(media_tree, tmp_path):
    ensure_first_run_files()
    config_file().write_text(f'libraries = ["{media_tree}"]\nlanguages = ["en"]\n')
    ctx = AppContext(db_path=tmp_path / "tui.db")
    fake = FakeProvider()
    ctx.provider = fake
    ctx.downloader = Downloader(ctx.db, fake, ctx.scanner, ctx.settings)
    ctx.worker.downloader = ctx.downloader
    return JsmApp(ctx=ctx)


async def wait_for_queue(app, pilot, timeout=50):
    for _ in range(timeout):
        await pilot.pause(0.1)
        jobs = app.ctx.db.jobs()
        if jobs and all(
            j.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.WAITING_QUOTA)
            for j in jobs
        ):
            return
    raise AssertionError(f"queue never settled: {app.ctx.db.jobs()}")


async def test_browse_select_download_flow(app, media_tree):
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        browser = app.screen
        assert isinstance(browser, BrowserScreen)
        browser.open_directory(str(media_tree / "new-movies"))
        await pilot.pause(0.8)  # let the background scan finish

        table = browser.query_one("#media-table", DataTable)
        assert table.row_count == 3

        # filter to missing only -> just Winnie
        await pilot.press("f")
        await pilot.pause(0.2)
        assert table.row_count == 1

        # select it and download without sync
        await pilot.press("space")
        assert len(browser.selected) == 1
        await pilot.press("o")
        await wait_for_queue(app, pilot)

        jobs = app.ctx.db.jobs()
        assert len(jobs) == 1
        assert all(j.status == JobStatus.COMPLETED for j in jobs)
        winnie = app.ctx.db.get_media_by_path(
            str(media_tree / "new-movies" / "Winnie The Pooh (2011).mkv")
        )
        assert winnie.status == MediaStatus.OK
        assert (media_tree / "new-movies" / "Winnie The Pooh (2011).eng.srt").read_bytes() == SRT


async def test_queue_screen_shows_jobs(app, media_tree):
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        app.screen.open_directory(str(media_tree / "new-movies"))
        await pilot.pause(0.8)
        await pilot.press("space", "o")
        await wait_for_queue(app, pilot)
        await pilot.press("3")
        await pilot.pause(0.4)
        table = app.screen.query_one("#queue-table", DataTable)
        assert table.row_count == 1


async def test_failed_job_shows_results_dialog(app, media_tree):
    """Failures must be surfaced in a modal, not silently swallowed."""
    from jsm.providers.opensubtitles import OpenSubtitlesError
    from jsm.tui.dialogs import JobResultsDialog

    app.ctx.provider.fail = OpenSubtitlesError("server exploded")
    app.ctx.downloader.provider = app.ctx.provider
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        app.screen.open_directory(str(media_tree / "new-movies"))
        await pilot.pause(0.8)
        await pilot.press("f", "space", "o")
        await wait_for_queue(app, pilot)
        await pilot.pause(0.5)
        assert isinstance(app.screen, JobResultsDialog)
        rows = app.screen.query(".result-row")
        assert any("server exploded" in str(r.render()) for r in rows)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert isinstance(app.screen, BrowserScreen)


async def test_hide_ok_toggle_declutters(app, media_tree):
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        browser = app.screen
        browser.open_directory(str(media_tree / "new-movies"))
        await pilot.pause(0.8)
        table = browser.query_one("#media-table", DataTable)
        assert table.row_count == 3  # Alien (OK), Winnie (missing), DuckTales (wrong)
        await pilot.press("h")
        await pilot.pause(0.2)
        assert table.row_count == 2  # Alien hidden
        assert browser.hide_ok is True
        await pilot.press("h")
        await pilot.pause(0.2)
        assert table.row_count == 3


async def test_bulk_dialog_requires_typed_phrase(app, media_tree):
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        app.screen.open_directory(str(media_tree / "new-movies"))
        await pilot.pause(0.8)
        await pilot.press("b")
        await pilot.pause(0.4)
        dialog = app.screen
        assert isinstance(dialog, BulkConfirmDialog)

        start = dialog.query_one("#bulk-start")
        assert start.disabled  # locked until the phrase is typed

        dialog.query_one("#bulk-confirm-input", Input).value = "DOWNLOAD ALL"
        await pilot.pause(0.2)
        assert not start.disabled

        await pilot.press("escape")
        await pilot.pause(0.3)
        assert isinstance(app.screen, BrowserScreen)
        assert app.ctx.db.jobs() == []  # cancel queued nothing
