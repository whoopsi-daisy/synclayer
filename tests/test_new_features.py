"""Tests for the feature set: Jellyfin filenames, username/password auth,
rate-limit handling, subscleaner cleanup, and scan progress."""

import httpx
import pytest

from jsm.config.settings import Settings
from jsm.database.models import JobAction, JobStatus
from jsm.providers.accounts import AccountManager
from jsm.providers.base import SubtitleCandidate
from jsm.providers.opensubtitles import (
    AuthError,
    NotConfiguredError,
    OpenSubtitlesProvider,
    RateLimitedError,
)
from jsm.subtitles import cleaner
from jsm.subtitles.downloader import Downloader
from jsm.subtitles.fileops import subtitle_destination
from jsm.subtitles.queue import QueueWorker
from tests.conftest import SRT, FakeProvider
from tests.test_opensubtitles import make_provider


# --- #5/#6 Jellyfin ISO 639-2/B output filenames ----------------------------

def test_subtitle_destination_uses_basename_and_three_letter_code(tmp_path):
    media = tmp_path / "Movie.mp4"
    # downloader passes the already-converted 639-2/B code
    assert subtitle_destination(media, "eng").name == "Movie.eng.srt"


async def test_download_writes_jellyfin_filename(db, scanner, tmp_path, fake_provider):
    (tmp_path / "Movie.mp4").write_bytes(b"x" * 200_000)
    scanner.scan(tmp_path)
    media = db.get_media_by_path(str(tmp_path / "Movie.mp4"))
    downloader = Downloader(db, fake_provider, scanner, Settings())
    outcome = await downloader.download_for(media, "en")  # internal 639-1
    assert outcome.success
    assert (tmp_path / "Movie.eng.srt").exists()          # Movie.mp4 -> Movie.eng.srt
    # internal DB language stays ISO 639-1
    langs = {s.language for s in db.subtitles_for(media.id)}
    assert "en" in langs


# --- #1 auth needs username/password accounts AND an API key ----------------

async def test_provider_not_configured_without_api_key(db):
    # The OpenSubtitles REST API rejects every request without an Api-Key
    # header, so accounts alone are not enough.
    provider = OpenSubtitlesProvider("", AccountManager(db, [("u", "p")]))
    assert provider.configured is False


async def test_provider_not_configured_without_accounts(db):
    provider = OpenSubtitlesProvider("key", AccountManager(db, []))
    assert provider.configured is False


async def test_validate_account_reports_bad_credentials(db):
    def handler(request):
        return httpx.Response(401)

    provider, _ = make_provider(db, handler, accounts=[("bob", "wrong")])
    ok, message = await provider.validate_account("bob")
    assert ok is False
    assert "credential" in message


async def test_validate_account_ok(db):
    def handler(request):
        return httpx.Response(200, json={"token": "t"})

    provider, _ = make_provider(db, handler, accounts=[("bob", "right")])
    ok, message = await provider.validate_account("bob")
    assert ok is True


# --- #7 rate limit / API error handling -------------------------------------

async def test_retries_on_429_then_succeeds(db, monkeypatch):
    import jsm.providers.opensubtitles as osmod

    slept = []
    monkeypatch.setattr(osmod, "_sleep", lambda s: slept.append(s) or _noop())
    state = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "t"})
        if request.url.path.endswith("/download"):
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"link": "https://f.test/s.srt"})
        return httpx.Response(200, content=b"data")

    provider, _ = make_provider(db, handler)
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="1",
                                  language="en", release_name="x")
    assert await provider.download(candidate) == b"data"
    assert slept == [1.0]  # honored Retry-After once


async def test_persistent_429_raises_rate_limited(db, monkeypatch):
    import jsm.providers.opensubtitles as osmod

    monkeypatch.setattr(osmod, "_sleep", lambda s: _noop())

    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(429)

    provider, _ = make_provider(db, handler)
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="1",
                                  language="en", release_name="x")
    with pytest.raises(RateLimitedError):
        await provider.download(candidate)


async def test_network_error_retried_then_raised(db, monkeypatch):
    import jsm.providers.opensubtitles as osmod

    monkeypatch.setattr(osmod, "_sleep", lambda s: _noop())

    def handler(request):
        raise httpx.ConnectError("no route")

    provider, _ = make_provider(db, handler)
    with pytest.raises(Exception):  # OpenSubtitlesError after retries
        await provider.search(["en"], query="x")


async def _noop():
    return None


# --- #3 subscleaner cleanup --------------------------------------------------

async def test_clean_reports_missing_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(cleaner, "subscleaner_available", lambda: False)
    sub = tmp_path / "m.eng.srt"
    sub.write_bytes(SRT)
    changed, message = await cleaner.clean(sub)
    assert changed is False
    assert "not installed" in message
    assert sub.read_bytes() == SRT  # untouched


async def test_clean_rewrites_and_backs_up(tmp_path, monkeypatch):
    monkeypatch.setattr(cleaner, "subscleaner_available", lambda: True)
    sub = tmp_path / "m.eng.srt"
    sub.write_bytes(b"line with advert\nreal line\n")

    async def fake_exec(*args, **kwargs):
        # subscleaner rewrites args[-1] (the temp copy) in place
        tmp = args[-1]

        class P:
            returncode = 0

            async def communicate(self):
                open(tmp, "wb").write(b"real line\n")
                return b"", b""

        return P()

    monkeypatch.setattr(cleaner.asyncio, "create_subprocess_exec", fake_exec)
    changed, message = await cleaner.clean(sub)
    assert changed is True
    assert sub.read_bytes() == b"real line\n"
    assert (tmp_path / "m.eng.srt.bak").read_bytes() == b"line with advert\nreal line\n"


async def test_clean_job_action(db, scanner, tmp_path, monkeypatch, fake_provider):
    monkeypatch.setattr(cleaner, "subscleaner_available", lambda: True)

    async def fake_clean(path):
        return True, "cleaned"

    monkeypatch.setattr(cleaner, "clean", fake_clean)
    (tmp_path / "Movie.mkv").write_bytes(b"x" * 200_000)
    (tmp_path / "Movie.eng.srt").write_bytes(SRT)
    scanner.scan(tmp_path)
    media = db.get_media_by_path(str(tmp_path / "Movie.mkv"))
    downloader = Downloader(db, fake_provider, scanner, Settings())
    worker = QueueWorker(db, downloader, AccountManager(db, [("u", "p")]))
    worker.enqueue(media.id, JobAction.CLEAN, "en")
    await worker.run_until_empty()
    assert db.jobs()[0].status == JobStatus.COMPLETED


async def test_clean_downloads_flag_runs_cleaner(db, scanner, tmp_path, monkeypatch, fake_provider):
    calls = []

    async def fake_clean(path):
        calls.append(path)
        return True, "cleaned"

    monkeypatch.setattr(cleaner, "clean", fake_clean)
    (tmp_path / "Movie.mkv").write_bytes(b"x" * 200_000)
    scanner.scan(tmp_path)
    media = db.get_media_by_path(str(tmp_path / "Movie.mkv"))
    downloader = Downloader(db, fake_provider, scanner, Settings())
    worker = QueueWorker(db, downloader, AccountManager(db, [("u", "p")]),
                         clean_downloads=True)
    worker.enqueue(media.id, JobAction.DOWNLOAD, "en")
    await worker.run_until_empty()
    assert db.jobs()[0].status == JobStatus.COMPLETED
    assert len(calls) == 1  # cleaner ran on the downloaded subtitle


# --- #8 scan progress callback ----------------------------------------------

def test_scan_progress_callback_fires(db, scanner, media_tree):
    seen = []
    scanner.scan(media_tree, on_progress=lambda stats, d: seen.append((stats.scanned, str(d))))
    assert seen  # at least one directory reported
    # last report has the final cumulative count
    assert seen[-1][0] == 4
