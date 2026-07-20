"""Central job queue.

Jobs are persisted in SQLite (they survive restarts) and processed by an
asyncio worker. Job lifecycle:

    queued -> searching -> downloading -> [syncing] -> completed
           -> failed | paused | waiting_quota (auto-resumes when quota refreshes)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from jsm.activity import ActivityLog
from jsm.database.db import Database
from jsm.database.models import JobAction, JobStatus, QueueJob, SyncStatus
from jsm.providers.accounts import AccountManager
from jsm.providers.opensubtitles import NotConfiguredError, OpenSubtitlesError, QuotaExceededError
from jsm.subtitles import cleaner, synchronizer
from jsm.subtitles.downloader import Downloader
from jsm.subtitles.language import language_name, normalize_language

UpdateCallback = Callable[[QueueJob], None]


def _name(job: QueueJob) -> str:
    return Path(job.media_path or "?").name


class QueueWorker:
    def __init__(
        self,
        db: Database,
        downloader: Downloader,
        accounts: AccountManager,
        on_update: UpdateCallback | None = None,
        idle_poll_seconds: float = 1.0,
        concurrency: int = 1,
        clean_downloads: bool = False,
        activity: ActivityLog | None = None,
    ):
        self.db = db
        self.downloader = downloader
        self.accounts = accounts
        self.on_update = on_update
        self.idle_poll_seconds = idle_poll_seconds
        self.concurrency = max(1, concurrency)
        # Run subscleaner on each freshly downloaded subtitle (from config).
        self.clean_downloads = clean_downloads
        # Optional shared activity log; a no-op sink keeps call sites simple.
        self.activity = activity or ActivityLog()
        self._stop = asyncio.Event()
        self._wakeup = asyncio.Event()

    # ------------------------------------------------------------- job control

    def enqueue(
        self,
        media_id: int,
        action: str,
        language: str,
        priority: int = 0,
        min_confidence: float = 0.0,
    ) -> QueueJob:
        language = normalize_language(language) or language
        job = self.db.enqueue(media_id, action, language, priority, min_confidence)
        self._notify(job)
        self._wakeup.set()
        return job

    def pause(self, job_id: int) -> None:
        self._set_status_if(job_id, JobStatus.QUEUED, JobStatus.PAUSED)

    def resume(self, job_id: int) -> None:
        self._set_status_if(job_id, JobStatus.PAUSED, JobStatus.QUEUED)
        self._wakeup.set()

    def retry(self, job_id: int) -> None:
        self._set_status_if(job_id, JobStatus.FAILED, JobStatus.QUEUED)
        self._wakeup.set()

    def reprioritize(self, job_id: int, priority: int) -> None:
        self.db.update_job(job_id, priority=priority)
        self._notify(self.db.get_job(job_id))

    def _set_status_if(self, job_id: int, expected: str, new: str) -> None:
        job = self.db.get_job(job_id)
        if job and job.status == expected:
            self.db.update_job(job_id, status=new)
            self._notify(self.db.get_job(job_id))

    def _notify(self, job: QueueJob | None) -> None:
        if job is not None and self.on_update is not None:
            self.on_update(job)

    def _update(self, job: QueueJob, **kwargs) -> None:
        assert job.id is not None
        self.db.update_job(job.id, **kwargs)
        self._notify(self.db.get_job(job.id))

    # -------------------------------------------------------------- processing

    def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()

    async def run_forever(self) -> None:
        self.db.reset_stuck_jobs()
        async with asyncio.TaskGroup() as tg:
            for _ in range(self.concurrency):
                tg.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            processed = await self.process_next()
            if not processed:
                self._requeue_quota_jobs_if_possible()
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(self._wakeup.wait(), self.idle_poll_seconds)
                except asyncio.TimeoutError:
                    pass

    async def run_until_empty(self) -> int:
        """Process everything currently queued (CLI mode). Returns job count."""
        count = 0
        self._requeue_quota_jobs_if_possible()
        while await self.process_next():
            count += 1
        return count

    def _requeue_quota_jobs_if_possible(self) -> None:
        if self.accounts.pick_best() is None:
            return
        waiting = [j for j in self.db.jobs() if j.status == JobStatus.WAITING_QUOTA]
        for job in waiting:
            assert job.id is not None
            self.db.update_job(job.id, status=JobStatus.QUEUED)
            self._notify(self.db.get_job(job.id))

    async def process_next(self) -> bool:
        job = self.db.next_queued_job()
        if job is None:
            return False
        await self._process(job)
        return True

    async def _process(self, job: QueueJob) -> None:
        media = self.db.get_media(job.media_id)
        if media is None:
            self._update(job, status=JobStatus.FAILED, error_message="Media no longer in database")
            return
        lang = language_name(job.language)
        try:
            if job.action in (JobAction.DOWNLOAD, JobAction.DOWNLOAD_SYNC):
                self._update(job, status=JobStatus.SEARCHING, detail="Searching…")
                self.activity.info(f"Searching {lang} subtitle for {_name(job)}")
                outcome = await self.downloader.download_for(
                    media, job.language, min_confidence=job.min_confidence
                )
                if not outcome.success:
                    self._update(job, status=JobStatus.FAILED, error_message=outcome.message)
                    self.activity.warn(f"No subtitle for {_name(job)}: {outcome.message}")
                    return
                self._update(job, status=JobStatus.DOWNLOADING, detail=outcome.message)
                self.activity.ok(f"Downloaded {lang} for {_name(job)}: {outcome.message}")
                if self.clean_downloads and outcome.subtitle_path:
                    await self._clean_file(job, outcome.subtitle_path)
                if job.action == JobAction.DOWNLOAD_SYNC and outcome.subtitle_path:
                    await self._sync_file(job, media.path, outcome.subtitle_path)
                elif outcome.subtitle_path:
                    self.activity.info(
                        f"Sync skipped for {_name(job)} (download-only)"
                    )
                self._update(job, status=JobStatus.COMPLETED)
            elif job.action == JobAction.SYNC:
                target = self._sync_target(job, media)
                if target is None:
                    msg = (
                        f"No external '{job.language}' subtitle file found "
                        f"next to {media.filename} - download one first, or "
                        "name it like the video (Movie.en.srt)"
                    )
                    self._update(job, status=JobStatus.FAILED, error_message=msg)
                    self.activity.warn(f"Sync failed for {_name(job)}: {msg}")
                    return
                self.activity.info(f"Syncing {Path(target).name} to {_name(job)}")
                await self._sync_file(job, media.path, target, fail_job=True)
            elif job.action == JobAction.CLEAN:
                target = self._sync_target(job, media)
                if target is None:
                    self._update(
                        job, status=JobStatus.FAILED,
                        error_message=f"No '{job.language}' subtitle to clean",
                    )
                    self.activity.warn(f"Nothing to clean for {_name(job)}")
                    return
                await self._clean_file(job, target)
                self._update(job, status=JobStatus.COMPLETED)
            else:
                self._update(job, status=JobStatus.FAILED, error_message=f"Unknown action {job.action}")
        except QuotaExceededError:
            when = self.accounts.next_available_time()
            detail = "All account quotas exhausted"
            if when:
                import datetime

                resume = datetime.datetime.fromtimestamp(when).strftime("%H:%M")
                detail += f" - auto-resumes around {resume}"
            self._update(job, status=JobStatus.WAITING_QUOTA, detail=detail)
            self.activity.warn(f"{_name(job)} parked: {detail}")
        except (NotConfiguredError, OpenSubtitlesError, OSError) as exc:
            self._update(job, status=JobStatus.FAILED, error_message=str(exc))
            self.activity.error(f"{_name(job)} failed: {exc}")
        except Exception as exc:  # never let one bad job kill the worker
            self._update(job, status=JobStatus.FAILED, error_message=f"{type(exc).__name__}: {exc}")
            self.activity.error(f"{_name(job)} failed: {type(exc).__name__}: {exc}")

    def _sync_target(self, job: QueueJob, media=None) -> str | None:
        # The database only knows what the last scan saw. A subtitle the user
        # just dropped next to the movie (the whole point of a manual sync)
        # would be missed - re-scan the file first so it is picked up.
        if media is not None:
            try:
                self.downloader.scanner.rescan_media(media)
            except OSError:
                pass  # unreadable now; fall back to whatever the DB has
        subs = self.db.subtitles_for(job.media_id)
        candidates = [
            s for s in subs
            if s.path and s.language in (job.language, "und")
        ]
        # Prefer freshly downloaded subs, then exact language over 'und'.
        candidates.sort(
            key=lambda s: (s.source == "downloaded", s.language == job.language),
            reverse=True,
        )
        return candidates[0].path if candidates else None

    async def _clean_file(self, job: QueueJob, subtitle_path: str) -> None:
        """Run subscleaner; a cleanup failure is never fatal to the job."""
        self._update(job, detail=f"Cleaning {Path(subtitle_path).name}")
        changed, message = await cleaner.clean(subtitle_path)
        self._update(job, detail=message)
        name = Path(subtitle_path).name
        if changed:
            self.activity.ok(f"Cleaned {name}: {message}")
        else:
            self.activity.info(f"Clean {name}: {message}")

    async def _sync_file(
        self, job: QueueJob, media_path: str, subtitle_path: str, fail_job: bool = False
    ) -> None:
        name = Path(subtitle_path).name
        self._update(job, status=JobStatus.SYNCING, detail=f"Syncing {name}")
        ok, message = await synchronizer.synchronize(media_path, subtitle_path)
        status = SyncStatus.SYNCED if ok else SyncStatus.SYNC_FAILED
        for sub in self.db.subtitles_for(job.media_id):
            if sub.path == subtitle_path and sub.id is not None:
                self.db.set_subtitle_sync_status(sub.id, status)
        media = self.db.get_media(job.media_id)
        if media is not None:
            self.downloader.scanner.rescan_media(media)
        if ok:
            self.activity.ok(f"Synced {name}: {message}")
            if fail_job:  # sync-only job: completing it is our responsibility
                self._update(job, status=JobStatus.COMPLETED, detail=message)
            else:
                self._update(job, detail=message)
        elif fail_job:
            self._update(job, status=JobStatus.FAILED, error_message=message)
            self.activity.error(f"Sync failed for {name}: {message}")
        else:
            self._update(job, detail=f"Downloaded, but sync failed: {message}")
            self.activity.warn(f"Downloaded {name}, but sync failed: {message}")
