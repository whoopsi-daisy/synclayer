import hashlib

import pytest

from jsm.config.settings import Settings
from jsm.database.models import JobAction, JobStatus, MediaStatus
from jsm.providers.accounts import AccountManager
from jsm.providers.opensubtitles import QuotaExceededError
from jsm.subtitles.downloader import Downloader
from jsm.subtitles.queue import QueueWorker
from tests.conftest import SRT, FakeProvider


def snapshot_media_bytes(root):
    return {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*") if p.suffix in {".mkv", ".mp4", ".avi", ".webm"}
    }


@pytest.fixture
def worker(db, scanner, media_tree, fake_provider):
    scanner.scan(media_tree)
    downloader = Downloader(db, fake_provider, scanner, Settings())
    accounts = AccountManager(db, [("u", "p")])
    return QueueWorker(db, downloader, accounts)


async def test_download_job_end_to_end(db, media_tree, worker):
    before = snapshot_media_bytes(media_tree)
    media = next(m for m in db.all_media() if "Winnie" in m.filename)
    worker.enqueue(media.id, JobAction.DOWNLOAD, "en")
    assert await worker.run_until_empty() == 1

    job = db.jobs()[0]
    assert job.status == JobStatus.COMPLETED
    sub_path = media_tree / "new-movies" / "Winnie The Pooh (2011).en.srt"
    assert sub_path.read_bytes() == SRT
    assert db.get_media(media.id).status == MediaStatus.OK
    # media files untouched, byte for byte
    assert snapshot_media_bytes(media_tree) == before


async def test_duplicate_enqueue_collapses(db, worker):
    media = db.all_media()[0]
    a = worker.enqueue(media.id, JobAction.DOWNLOAD, "en")
    b = worker.enqueue(media.id, JobAction.DOWNLOAD, "en")
    assert a.id == b.id


async def test_second_download_never_overwrites(db, media_tree, worker):
    media = next(m for m in db.all_media() if "Winnie" in m.filename)
    worker.enqueue(media.id, JobAction.DOWNLOAD, "en")
    await worker.run_until_empty()
    first = media_tree / "new-movies" / "Winnie The Pooh (2011).en.srt"
    first.write_bytes(b"user edited this file")
    worker.enqueue(media.id, JobAction.DOWNLOAD, "en")
    await worker.run_until_empty()
    assert first.read_bytes() == b"user edited this file"
    assert (media_tree / "new-movies" / "Winnie The Pooh (2011).en.2.srt").exists()


async def test_min_confidence_blocks_weak_matches(db, scanner, media_tree):
    from jsm.providers.base import SubtitleCandidate

    scanner.scan(media_tree)
    weak = FakeProvider(candidates=[
        SubtitleCandidate(provider="fake", file_id="9", language="en",
                          release_name="Unrelated Movie 1993")
    ])
    downloader = Downloader(db, weak, scanner, Settings())
    worker = QueueWorker(db, downloader, AccountManager(db, [("u", "p")]))
    media = next(m for m in db.all_media() if "Winnie" in m.filename)
    worker.enqueue(media.id, JobAction.DOWNLOAD, "en", min_confidence=0.99)
    await worker.run_until_empty()
    job = db.jobs()[0]
    assert job.status == JobStatus.FAILED
    assert "confidence" in job.error_message


async def test_quota_exhaustion_parks_job(db, scanner, media_tree):
    scanner.scan(media_tree)
    provider = FakeProvider(fail=QuotaExceededError("spent"))
    downloader = Downloader(db, provider, scanner, Settings())
    accounts = AccountManager(db, [("u", "p")], daily_limit=1)
    accounts.record_download("u")
    worker = QueueWorker(db, downloader, accounts)
    media = db.all_media()[0]
    worker.enqueue(media.id, JobAction.DOWNLOAD, "en")
    await worker.run_until_empty()
    assert db.jobs()[0].status == JobStatus.WAITING_QUOTA


async def test_pause_resume_retry(db, worker):
    media = db.all_media()[0]
    job = worker.enqueue(media.id, JobAction.DOWNLOAD, "en")
    worker.pause(job.id)
    assert db.get_job(job.id).status == JobStatus.PAUSED
    assert await worker.run_until_empty() == 0  # paused job is skipped
    worker.resume(job.id)
    assert db.get_job(job.id).status == JobStatus.QUEUED


async def test_priority_order(db, worker):
    media = db.all_media()
    low = worker.enqueue(media[0].id, JobAction.DOWNLOAD, "en", priority=0)
    high = worker.enqueue(media[1].id, JobAction.DOWNLOAD, "en", priority=5)
    assert db.next_queued_job().id == high.id


async def test_sync_job_without_ffsubsync_fails_cleanly(db, media_tree, worker, monkeypatch):
    media = next(m for m in db.all_media() if "Alien" in m.filename)
    worker.enqueue(media.id, JobAction.SYNC, "en")
    await worker.run_until_empty()
    job = db.jobs()[0]
    assert job.status == JobStatus.FAILED
    assert "ffsubsync" in job.error_message
    # the subtitle file itself is untouched
    assert (media_tree / "new-movies" / "Alien (1979).en.srt").read_bytes() == SRT


async def test_dry_run_writes_nothing(db, media_tree, scanner, fake_provider):
    scanner.scan(media_tree)
    downloader = Downloader(db, fake_provider, scanner, Settings())
    media = next(m for m in db.all_media() if "Winnie" in m.filename)
    outcome = await downloader.download_for(media, "en", dry_run=True)
    assert outcome.success
    assert outcome.dry_run
    assert "[dry-run]" in outcome.message
    assert not (media_tree / "new-movies" / "Winnie The Pooh (2011).en.srt").exists()
    assert fake_provider.download_count == 0
