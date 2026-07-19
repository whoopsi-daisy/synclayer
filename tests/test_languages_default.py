"""Primary/secondary language selection and the download->clean->sync default."""

import pytest

from jsm.config.settings import Settings
from jsm.database.models import JobAction, JobStatus, MediaStatus
from jsm.main import _languages
from jsm.providers.accounts import AccountManager
from jsm.subtitles.downloader import Downloader
from jsm.subtitles.queue import QueueWorker
from tests.conftest import SRT, FakeProvider


class Ctx:
    def __init__(self, languages):
        self.settings = Settings(languages=languages)


def test_default_is_primary_only():
    ctx = Ctx(["en", "sv"])
    assert _languages(ctx, None, both=False) == ["en"]


def test_both_returns_all_configured():
    ctx = Ctx(["en", "sv"])
    assert _languages(ctx, None, both=True) == ["en", "sv"]


def test_explicit_override_comma_list_wins():
    ctx = Ctx(["en", "sv"])
    assert _languages(ctx, "sv", both=False) == ["sv"]
    assert _languages(ctx, "eng,swe", both=True) == ["en", "sv"]  # normalized + deduped


def test_defaults_enable_full_pipeline():
    s = Settings()
    assert s.primary_language == "en"
    assert s.secondary_languages == ["sv"]
    assert s.sync_by_default is True
    assert s.clean_by_default is True


async def test_both_downloads_two_languages_jellyfin_named(db, scanner, tmp_path, monkeypatch):
    from jsm.subtitles import cleaner

    monkeypatch.setattr(cleaner, "clean", _fake_clean)
    (tmp_path / "Movie.mkv").write_bytes(b"x" * 200_000)
    scanner.scan(tmp_path)
    media = db.get_media_by_path(str(tmp_path / "Movie.mkv"))
    downloader = Downloader(db, FakeProvider(), scanner, Settings())
    worker = QueueWorker(db, downloader, AccountManager(db, [("u", "p")]),
                         clean_downloads=True)
    for lang in ("en", "sv"):
        worker.enqueue(media.id, JobAction.DOWNLOAD, lang)
    await worker.run_until_empty()
    # Two Jellyfin-named sidecars, ISO 639-2/B codes.
    assert (tmp_path / "Movie.eng.srt").exists()
    assert (tmp_path / "Movie.swe.srt").exists()
    assert all(j.status == JobStatus.COMPLETED for j in db.jobs())


async def _fake_clean(path):
    return False, "nothing to clean"
