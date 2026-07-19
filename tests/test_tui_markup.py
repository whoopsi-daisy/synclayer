"""Bracketed filenames must render literally, never as Rich markup."""

import pytest
from rich.text import Text
from textual.widgets import DataTable

from jsm.config.settings import config_file, ensure_first_run_files
from jsm.core import AppContext
from jsm.subtitles.downloader import Downloader
from jsm.tui.app import JsmApp
from tests.conftest import SRT, FakeProvider

BRACKETED = "[SubsPlease] Show - 01 (1080p).mkv"


@pytest.fixture
def bracket_app(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / BRACKETED).write_bytes(b"x" * 200_000)
    (root / "[Group] Other [v2].mkv").write_bytes(b"y" * 200_000)
    ensure_first_run_files()
    config_file().write_text(f'libraries = ["{root}"]\nlanguages = ["en"]\n')
    ctx = AppContext(db_path=tmp_path / "markup.db")
    fake = FakeProvider()
    ctx.provider = fake
    ctx.downloader = Downloader(ctx.db, fake, ctx.scanner, ctx.settings)
    ctx.worker.downloader = ctx.downloader
    return JsmApp(ctx=ctx), root


async def test_bracketed_filenames_render_literally(bracket_app):
    app, root = bracket_app
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        browser = app.screen
        browser.open_directory(str(root))
        await pilot.pause(0.8)

        table = browser.query_one("#media-table", DataTable)
        assert table.row_count == 2
        names = set()
        for row_key in table.rows:
            cell = table.get_cell(row_key, browser._columns[1])
            assert isinstance(cell, Text)  # literal rendering, no markup parse
            names.add(cell.plain)
        assert BRACKETED in names  # '[SubsPlease]' prefix not swallowed

        # details view for a bracketed file must not raise MarkupError
        await pilot.press("v")
        await pilot.pause(0.4)
        assert type(app.screen).__name__ == "DetailsScreen"
        await pilot.press("escape")
        await pilot.pause(0.2)


async def test_filter_change_prunes_hidden_selection(bracket_app, tmp_path):
    app, root = bracket_app
    (root / "Has Sub (2020).mkv").write_bytes(b"z" * 200_000)
    (root / "Has Sub (2020).en.srt").write_bytes(SRT)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        browser = app.screen
        browser.open_directory(str(root))
        await pilot.pause(0.8)
        await pilot.press("ctrl+a")
        await pilot.pause(0.2)
        assert len(browser.selected) == 3
        # 'Has Sub' is OK; the missing-filter hides it, so it must leave the
        # selection - the shown count has to match what actions target.
        await pilot.press("f")
        await pilot.pause(0.3)
        assert len(browser.selected) == 2
        assert all(
            browser._media_cache[mid].status == "missing" for mid in browser.selected
        )
