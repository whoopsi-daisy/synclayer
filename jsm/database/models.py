"""Dataclasses mirroring the SQLite schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class MediaStatus(StrEnum):
    """Subtitle health of a media file for the wanted languages."""

    OK = "ok"                  # has a subtitle in a wanted language
    MISSING = "missing"        # no subtitle at all in a wanted language
    WRONG_LANG = "wrong_lang"  # has subtitles, but none in a wanted language
    UNSYNCED = "unsynced"      # has a wanted-language subtitle flagged unsynced


class SyncStatus(StrEnum):
    UNKNOWN = "unknown"
    SYNCED = "synced"
    UNSYNCED = "unsynced"
    SYNC_FAILED = "sync_failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    SYNCING = "syncing"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    WAITING_QUOTA = "waiting_quota"


class JobAction(StrEnum):
    DOWNLOAD = "download"
    DOWNLOAD_SYNC = "download_sync"
    SYNC = "sync"


ACTIVE_JOB_STATUSES = (
    JobStatus.QUEUED,
    JobStatus.SEARCHING,
    JobStatus.DOWNLOADING,
    JobStatus.SYNCING,
    JobStatus.WAITING_QUOTA,
)


@dataclass
class Media:
    id: int | None
    path: str
    filename: str
    directory: str
    size: int
    mtime: float
    hash: str | None = None
    duration: float | None = None
    scan_date: str | None = None
    status: str = MediaStatus.MISSING


@dataclass
class Subtitle:
    id: int | None
    media_id: int
    language: str
    path: str | None          # None for embedded streams
    source: str               # external | embedded | downloaded
    forced: bool = False
    hearing_impaired: bool = False
    downloaded_date: str | None = None
    sync_status: str = SyncStatus.UNKNOWN


@dataclass
class QueueJob:
    id: int | None
    media_id: int
    action: str
    language: str
    status: str = JobStatus.QUEUED
    priority: int = 0
    error_message: str | None = None
    detail: str | None = None       # human-readable progress note
    created: str | None = None
    updated: str | None = None
    # joined columns, populated by queries for display purposes
    media_path: str | None = field(default=None, compare=False)
