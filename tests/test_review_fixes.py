"""Regression tests for the bugs found in the post-merge code review."""

import time

import httpx
import pytest

from jsm.config.settings import Settings
from jsm.database.models import JobAction, JobStatus, Media, MediaStatus, Subtitle
from jsm.providers.accounts import AccountManager
from jsm.providers.base import SubtitleCandidate
from jsm.scanner.filesystem import Scanner, compute_status
from jsm.subtitles.downloader import Downloader
from jsm.subtitles.language import parse_subtitle_filename
from jsm.subtitles.matcher import rank_candidates
from jsm.subtitles.queue import QueueWorker
from tests.conftest import SRT, FakeProvider
from tests.test_opensubtitles import make_provider


# --- titles containing language words (The Italian Job) ---------------------

def test_title_language_word_not_misparsed():
    stem = "The.Italian.Job"
    assert parse_subtitle_filename("The.Italian.Job.srt", media_stem=stem) == (
        None, False, False,
    )
    assert parse_subtitle_filename("The.Italian.Job.en.srt", media_stem=stem) == (
        "en", False, False,
    )


def test_scan_italian_job_is_ok(db, tmp_path):
    scanner = Scanner(db, ["en"])
    (tmp_path / "The.Italian.Job.mkv").write_bytes(b"x" * 200_000)
    (tmp_path / "The.Italian.Job.srt").write_bytes(SRT)
    scanner.scan(tmp_path)
    media = db.get_media_by_path(str(tmp_path / "The.Italian.Job.mkv"))
    assert media.status == MediaStatus.OK  # was WRONG_LANG before the fix


# --- untagged embedded streams must not mark a file OK ----------------------

def test_untagged_embedded_stream_does_not_satisfy_wanted_language():
    embedded = Subtitle(id=None, media_id=1, language="und", path=None, source="embedded")
    assert compute_status([embedded], ["en"]) == MediaStatus.WRONG_LANG
    external = Subtitle(id=None, media_id=1, language="und", path="/m.srt", source="external")
    assert compute_status([external], ["en"]) == MediaStatus.OK


# --- transient stat failure must not delete DB history ----------------------

def test_media_under_escapes_like_wildcards(db):
    for directory in ("/media/Movies_HD", "/media/MoviesXHD", "/media/50%off"):
        db.upsert_media(Media(
            id=None, path=f"{directory}/film.mkv", filename="film.mkv",
            directory=directory, size=1, mtime=1.0,
        ))
    found = {m.directory for m in db.media_under("/media/Movies_HD")}
    assert found == {"/media/Movies_HD"}
    found = {m.directory for m in db.media_under("/media/50%off")}
    assert found == {"/media/50%off"}


# --- config languages like "eng" must still match candidates ----------------

def test_rank_candidates_normalizes_wanted_language():
    cand = SubtitleCandidate(provider="fake", file_id="1", language="en",
                             release_name="Inception 2010")
    for wanted in ("eng", "English", "en"):
        assert rank_candidates("Inception.2010.mkv", [cand], language=wanted) == [cand]


async def test_worker_normalizes_job_language(db, scanner, media_tree, fake_provider):
    scanner.scan(media_tree)
    downloader = Downloader(db, fake_provider, scanner, Settings())
    worker = QueueWorker(db, downloader, AccountManager(db, [("u", "p")]))
    media = next(m for m in db.all_media() if "Winnie" in m.filename)
    job = worker.enqueue(media.id, JobAction.DOWNLOAD, "eng")
    assert job.language == "en"
    await worker.run_until_empty()
    assert db.get_job(job.id).status == JobStatus.COMPLETED


# --- download provenance survives a rescan ----------------------------------

async def test_downloaded_source_survives_rescan(db, scanner, media_tree, fake_provider):
    scanner.scan(media_tree)
    downloader = Downloader(db, fake_provider, scanner, Settings())
    media = next(m for m in db.all_media() if "Winnie" in m.filename)
    outcome = await downloader.download_for(media, "en")
    assert outcome.success
    scanner.scan(media_tree)  # a later full rescan must keep provenance too
    subs = [s for s in db.subtitles_for(media.id) if s.path == outcome.subtitle_path]
    assert len(subs) == 1
    assert subs[0].source == "downloaded"
    assert subs[0].downloaded_date is not None


# --- server-side 406 marks the account exhausted and rotates ----------------

def test_mark_exhausted_zeroes_quota(db):
    mgr = AccountManager(db, [("a", "pa")])
    mgr.record_download("a")
    mgr.mark_exhausted("a")
    assert mgr.quota("a").remaining == 0
    assert mgr.pick_best() is None
    assert mgr.next_available_time() is not None


async def test_406_rotates_to_next_account_and_syncs_local_quota(db):
    def handler(request):
        if request.url.path.endswith("/login"):
            import json

            handler.last_user = json.loads(request.content)["username"]
            return httpx.Response(200, json={"token": f"t-{handler.last_user}"})
        if request.url.path.endswith("/download"):
            if handler.last_user == "a":
                return httpx.Response(406)  # server says account a is spent
            return httpx.Response(200, json={"link": "https://files.test/s.srt"})
        return httpx.Response(200, content=b"data")

    handler.last_user = None
    provider, mgr = make_provider(db, handler, accounts=[("a", "pa"), ("b", "pb")])
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="1",
                                  language="en", release_name="x")
    assert await provider.download(candidate) == b"data"
    assert mgr.quota("a").remaining == 0     # local tracking synced to server
    assert mgr.quota("b").used == 1


# --- re-enqueue updates priority / confidence on the active job -------------

def test_reenqueue_updates_priority_and_confidence(db, scanner, media_tree):
    scanner.scan(media_tree)
    media = db.all_media()[0]
    first = db.enqueue(media.id, JobAction.DOWNLOAD, "en", priority=0,
                       min_confidence=0.99)
    second = db.enqueue(media.id, JobAction.DOWNLOAD, "en", priority=5,
                        min_confidence=0.5)
    assert second.id == first.id
    assert second.priority == 5
    assert second.min_confidence == 0.5


# --- queue concurrency is honored -------------------------------------------

def test_worker_concurrency_wired(db, scanner, fake_provider):
    downloader = Downloader(db, fake_provider, scanner, Settings())
    worker = QueueWorker(db, downloader, AccountManager(db, []), concurrency=4)
    assert worker.concurrency == 4
    worker = QueueWorker(db, downloader, AccountManager(db, []), concurrency=0)
    assert worker.concurrency == 1  # clamped


# --- batch subtitle fetch matches per-row fetch -----------------------------

def test_subtitles_by_media_batch(db, scanner, media_tree):
    scanner.scan(media_tree)
    ids = [m.id for m in db.all_media()]
    batch = db.subtitles_by_media(ids)
    for media_id in ids:
        assert [s.path for s in batch[media_id]] == [
            s.path for s in db.subtitles_for(media_id)
        ]
    assert db.subtitles_by_media([]) == {}
