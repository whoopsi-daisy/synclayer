"""Central job queue.

Jobs are persisted in SQLite (they survive restarts) and processed by an
asyncio worker. Job lifecycle:

    queued -> searching -> downloading -> [syncing] -> completed
           -> failed | paused | waiting_quota (auto-resumes when quota refreshes)
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from jsm.database.db import Database
from jsm.database.models import JobAction, JobStatus, QueueJob, SyncStatus
from jsm.providers.accounts import AccountManager
from jsm.providers.opensubtitles import NotConfiguredError, OpenSubtitlesError, QuotaExceededError
from jsm.subtitles import cleaner, synchronizer
from jsm.subtitles.downloader import Downloader
from jsm.subtitles.language import normalize_language

UpdateCallback = Callable[[QueueJob], None]


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
    ):
        self.db = db
        self.downloader = downloader
        self.accounts = accounts
        self.on_update = on_update
        self.idle_poll_seconds = idle_poll_seconds
        self.concurrency = max(1, concurrency)
        # Run subscleaner on each freshly downloaded subtitle (from config).
        self.clean_downloads = clean_downloads
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
        try:
            if job.action in (JobAction.DOWNLOAD, JobAction.DOWNLOAD_SYNC):
                self._update(job, status=JobStatus.SEARCHING, detail="Searching…")
                outcome = await self.downloader.download_for(
                    media, job.language, min_confidence=job.min_confidence
                )
                if not outcome.success:
                    self._update(job, status=JobStatus.FAILED, error_message=outcome.message)
                    return
                self._update(job, status=JobStatus.DOWNLOADING, detail=outcome.message)
                if self.clean_downloads and outcome.subtitle_path:
                    await self._clean_file(job, outcome.subtitle_path)
                if job.action == JobAction.DOWNLOAD_SYNC and outcome.subtitle_path:
                    await self._sync_file(job, media.path, outcome.subtitle_path)
                self._update(job, status=JobStatus.COMPLETED)
            elif job.action == JobAction.SYNC:
                target = self._sync_target(job)
                if target is None:
                    self._update(
                        job, status=JobStatus.FAILED,
                        error_message=f"No external '{job.language}' subtitle to sync",
                    )
                    return
                await self._sync_file(job, media.path, target, fail_job=True)
            elif job.action == JobAction.CLEAN:
                target = self._sync_target(job)
                if target is None:
                    self._update(
                        job, status=JobStatus.FAILED,
                        error_message=f"No '{job.language}' subtitle to clean",
                    )
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
        except (NotConfiguredError, OpenSubtitlesError, OSError) as exc:
            self._update(job, status=JobStatus.FAILED, error_message=str(exc))
        except Exception as exc:  # never let one bad job kill the worker
            self._update(job, status=JobStatus.FAILED, error_message=f"{type(exc).__name__}: {exc}")

    def _sync_target(self, job: QueueJob) -> str | None:
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
        self._update(job, detail=f"Cleaning {subtitle_path}")
        changed, message = await cleaner.clean(subtitle_path)
        self._update(job, detail=message)

    async def _sync_file(
        self, job: QueueJob, media_path: str, subtitle_path: str, fail_job: bool = False
    ) -> None:
        self._update(job, status=JobStatus.SYNCING, detail=f"Syncing {subtitle_path}")
        ok, message = await synchronizer.synchronize(media_path, subtitle_path)
        status = SyncStatus.SYNCED if ok else SyncStatus.SYNC_FAILED
        for sub in self.db.subtitles_for(job.media_id):
            if sub.path == subtitle_path and sub.id is not None:
                self.db.set_subtitle_sync_status(sub.id, status)
        media = self.db.get_media(job.media_id)
        if media is not None:
            self.downloader.scanner.rescan_media(media)
        if ok:
            if fail_job:  # sync-only job: completing it is our responsibility
                self._update(job, status=JobStatus.COMPLETED, detail=message)
            else:
                self._update(job, detail=message)
        elif fail_job:
            self._update(job, status=JobStatus.FAILED, error_message=message)
        else:
            self._update(job, detail=f"Downloaded, but sync failed: {message}")
