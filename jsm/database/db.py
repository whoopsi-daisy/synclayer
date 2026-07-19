"""SQLite persistence layer.

A single ``Database`` object owns one connection (WAL mode, so the TUI worker
and queries interleave safely) and exposes small repository methods for each
table. Designed for 10k+ media rows: paths and directories are indexed, and
browsing queries are always scoped to one directory.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from jsm.database.models import (
    ACTIVE_JOB_STATUSES,
    JobStatus,
    Media,
    MediaStatus,
    QueueJob,
    Subtitle,
    SyncStatus,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    directory TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    hash TEXT,
    duration REAL,
    scan_date TEXT,
    status TEXT NOT NULL DEFAULT 'missing'
);
CREATE INDEX IF NOT EXISTS idx_media_directory ON media(directory);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);

CREATE TABLE IF NOT EXISTS subtitles (
    id INTEGER PRIMARY KEY,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    language TEXT NOT NULL,
    path TEXT,
    source TEXT NOT NULL,
    forced INTEGER NOT NULL DEFAULT 0,
    hearing_impaired INTEGER NOT NULL DEFAULT 0,
    downloaded_date TEXT,
    sync_status TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_subtitles_media ON subtitles(media_id);

CREATE TABLE IF NOT EXISTS accounts (
    username TEXT PRIMARY KEY,
    download_timestamps TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    language TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    min_confidence REAL NOT NULL DEFAULT 0,
    error_message TEXT,
    detail TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            from jsm.config.settings import database_file

            path = database_file()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self.conn.executescript(_SCHEMA)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ media

    @staticmethod
    def _media_from_row(row: sqlite3.Row) -> Media:
        return Media(
            id=row["id"], path=row["path"], filename=row["filename"],
            directory=row["directory"], size=row["size"], mtime=row["mtime"],
            hash=row["hash"], duration=row["duration"],
            scan_date=row["scan_date"], status=row["status"],
        )

    def upsert_media(self, media: Media) -> Media:
        cur = self.conn.execute(
            """INSERT INTO media (path, filename, directory, size, mtime, hash,
                                  duration, scan_date, status)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 filename=excluded.filename, directory=excluded.directory,
                 size=excluded.size, mtime=excluded.mtime,
                 hash=excluded.hash, duration=excluded.duration,
                 scan_date=excluded.scan_date, status=excluded.status
            """,
            (media.path, media.filename, media.directory, media.size,
             media.mtime, media.hash, media.duration,
             media.scan_date or _now(), media.status),
        )
        self.conn.commit()
        if media.id is None:
            row = self.conn.execute(
                "SELECT id FROM media WHERE path=?", (media.path,)
            ).fetchone()
            media.id = row["id"]
        return media

    def get_media(self, media_id: int) -> Media | None:
        row = self.conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        return self._media_from_row(row) if row else None

    def get_media_by_path(self, path: str) -> Media | None:
        row = self.conn.execute("SELECT * FROM media WHERE path=?", (path,)).fetchone()
        return self._media_from_row(row) if row else None

    def media_in_directory(self, directory: str) -> list[Media]:
        rows = self.conn.execute(
            "SELECT * FROM media WHERE directory=? ORDER BY filename", (directory,)
        ).fetchall()
        return [self._media_from_row(r) for r in rows]

    def media_under(self, prefix: str, status: str | None = None) -> list[Media]:
        """All media whose path lives under *prefix* (recursively)."""
        prefix = prefix.rstrip("/")
        sql = "SELECT * FROM media WHERE (directory=? OR directory LIKE ?)"
        params: list = [prefix, prefix + "/%"]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY path"
        return [self._media_from_row(r) for r in self.conn.execute(sql, params)]

    def all_media(self, status: str | None = None) -> list[Media]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM media WHERE status=? ORDER BY path", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM media ORDER BY path").fetchall()
        return [self._media_from_row(r) for r in rows]

    def delete_media_not_in(self, directory: str, keep_paths: set[str]) -> int:
        """Prune DB rows for files that vanished from *directory* (non-recursive)."""
        rows = self.conn.execute(
            "SELECT id, path FROM media WHERE directory=?", (directory,)
        ).fetchall()
        stale = [r["id"] for r in rows if r["path"] not in keep_paths]
        for media_id in stale:
            self.conn.execute("DELETE FROM media WHERE id=?", (media_id,))
        if stale:
            self.conn.commit()
        return len(stale)

    def set_media_hash(self, media_id: int, hash_: str) -> None:
        self.conn.execute("UPDATE media SET hash=? WHERE id=?", (hash_, media_id))
        self.conn.commit()

    def set_media_status(self, media_id: int, status: str) -> None:
        self.conn.execute("UPDATE media SET status=? WHERE id=?", (status, media_id))
        self.conn.commit()

    def media_stats(self) -> dict[str, int]:
        stats = {s.value: 0 for s in MediaStatus}
        for row in self.conn.execute("SELECT status, COUNT(*) n FROM media GROUP BY status"):
            stats[row["status"]] = row["n"]
        stats["total"] = sum(stats.values())
        return stats

    # -------------------------------------------------------------- subtitles

    @staticmethod
    def _subtitle_from_row(row: sqlite3.Row) -> Subtitle:
        return Subtitle(
            id=row["id"], media_id=row["media_id"], language=row["language"],
            path=row["path"], source=row["source"], forced=bool(row["forced"]),
            hearing_impaired=bool(row["hearing_impaired"]),
            downloaded_date=row["downloaded_date"], sync_status=row["sync_status"],
        )

    def replace_subtitles(self, media_id: int, subtitles: list[Subtitle]) -> None:
        """Replace the recorded subtitle rows for one media file (scan result)."""
        old = {
            (r["path"], r["source"], r["language"]): r
            for r in self.conn.execute(
                "SELECT * FROM subtitles WHERE media_id=?", (media_id,)
            )
        }
        self.conn.execute("DELETE FROM subtitles WHERE media_id=?", (media_id,))
        for sub in subtitles:
            prev = old.get((sub.path, sub.source, sub.language))
            if prev is not None:
                # keep history the scanner cannot know about
                if sub.sync_status == SyncStatus.UNKNOWN:
                    sub.sync_status = prev["sync_status"]
                sub.downloaded_date = sub.downloaded_date or prev["downloaded_date"]
            self.add_subtitle(sub, commit=False)
        self.conn.commit()

    def add_subtitle(self, sub: Subtitle, commit: bool = True) -> Subtitle:
        cur = self.conn.execute(
            """INSERT INTO subtitles (media_id, language, path, source, forced,
                                      hearing_impaired, downloaded_date, sync_status)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sub.media_id, sub.language, sub.path, sub.source, int(sub.forced),
             int(sub.hearing_impaired), sub.downloaded_date, sub.sync_status),
        )
        sub.id = cur.lastrowid
        if commit:
            self.conn.commit()
        return sub

    def subtitles_for(self, media_id: int) -> list[Subtitle]:
        rows = self.conn.execute(
            "SELECT * FROM subtitles WHERE media_id=? ORDER BY language", (media_id,)
        ).fetchall()
        return [self._subtitle_from_row(r) for r in rows]

    def set_subtitle_sync_status(self, subtitle_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE subtitles SET sync_status=? WHERE id=?", (status, subtitle_id)
        )
        self.conn.commit()

    # --------------------------------------------------------------- accounts

    def account_timestamps(self, username: str) -> list[float]:
        row = self.conn.execute(
            "SELECT download_timestamps FROM accounts WHERE username=?", (username,)
        ).fetchone()
        if not row:
            return []
        try:
            return list(json.loads(row["download_timestamps"]))
        except (ValueError, TypeError):
            return []

    def record_account_download(self, username: str, when: float | None = None) -> None:
        when = time.time() if when is None else when
        stamps = self.account_timestamps(username)
        cutoff = time.time() - 24 * 3600
        stamps = [t for t in stamps if t > cutoff]
        stamps.append(when)
        self.conn.execute(
            """INSERT INTO accounts (username, download_timestamps) VALUES (?,?)
               ON CONFLICT(username) DO UPDATE SET
                 download_timestamps=excluded.download_timestamps""",
            (username, json.dumps(stamps)),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ queue

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> QueueJob:
        keys = row.keys()
        return QueueJob(
            id=row["id"], media_id=row["media_id"], action=row["action"],
            language=row["language"], status=row["status"], priority=row["priority"],
            min_confidence=row["min_confidence"],
            error_message=row["error_message"], detail=row["detail"],
            created=row["created"], updated=row["updated"],
            media_path=row["media_path"] if "media_path" in keys else None,
        )

    def enqueue(
        self,
        media_id: int,
        action: str,
        language: str,
        priority: int = 0,
        min_confidence: float = 0.0,
    ) -> QueueJob:
        # Avoid duplicate active jobs for the same file+action+language.
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        row = self.conn.execute(
            f"""SELECT q.*, m.path AS media_path FROM queue q
                JOIN media m ON m.id=q.media_id
                WHERE q.media_id=? AND q.action=? AND q.language=?
                  AND q.status IN ({placeholders})""",
            (media_id, action, language, *[s.value for s in ACTIVE_JOB_STATUSES]),
        ).fetchone()
        if row:
            return self._job_from_row(row)
        now = _now()
        cur = self.conn.execute(
            """INSERT INTO queue (media_id, action, language, status, priority,
                                  min_confidence, created, updated)
               VALUES (?,?,?,?,?,?,?,?)""",
            (media_id, action, language, JobStatus.QUEUED, priority,
             min_confidence, now, now),
        )
        self.conn.commit()
        return self.get_job(cur.lastrowid)  # type: ignore[return-value]

    def get_job(self, job_id: int) -> QueueJob | None:
        row = self.conn.execute(
            """SELECT q.*, m.path AS media_path FROM queue q
               JOIN media m ON m.id=q.media_id WHERE q.id=?""",
            (job_id,),
        ).fetchone()
        return self._job_from_row(row) if row else None

    def next_queued_job(self) -> QueueJob | None:
        row = self.conn.execute(
            """SELECT q.*, m.path AS media_path FROM queue q
               JOIN media m ON m.id=q.media_id
               WHERE q.status=? ORDER BY q.priority DESC, q.id LIMIT 1""",
            (JobStatus.QUEUED,),
        ).fetchone()
        return self._job_from_row(row) if row else None

    def jobs(self, limit: int = 200) -> list[QueueJob]:
        rows = self.conn.execute(
            """SELECT q.*, m.path AS media_path FROM queue q
               JOIN media m ON m.id=q.media_id
               ORDER BY CASE WHEN q.status IN ('queued','searching','downloading',
                                               'syncing','waiting_quota')
                             THEN 0 ELSE 1 END,
                        q.priority DESC, q.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self._job_from_row(r) for r in rows]

    def update_job(
        self,
        job_id: int,
        status: str | None = None,
        error_message: str | None = None,
        detail: str | None = None,
        priority: int | None = None,
    ) -> None:
        sets, params = ["updated=?"], [_now()]
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if error_message is not None:
            sets.append("error_message=?")
            params.append(error_message)
        if detail is not None:
            sets.append("detail=?")
            params.append(detail)
        if priority is not None:
            sets.append("priority=?")
            params.append(priority)
        params.append(job_id)
        self.conn.execute(f"UPDATE queue SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()

    def reset_stuck_jobs(self) -> None:
        """On startup, re-queue jobs left mid-flight by a previous run."""
        self.conn.execute(
            """UPDATE queue SET status=? WHERE status IN (?,?,?)""",
            (JobStatus.QUEUED, JobStatus.SEARCHING, JobStatus.DOWNLOADING,
             JobStatus.SYNCING),
        )
        self.conn.commit()

    def clear_finished_jobs(self) -> int:
        cur = self.conn.execute(
            "DELETE FROM queue WHERE status IN (?,?)",
            (JobStatus.COMPLETED, JobStatus.FAILED),
        )
        self.conn.commit()
        return cur.rowcount
