"""Download orchestration: hash -> search -> rank -> fetch -> safe atomic save.

Never touches media files except to read them (hash computation), and all
writes go through :mod:`jsm.subtitles.fileops`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from jsm.config.settings import Settings
from jsm.database.db import Database, _now
from jsm.database.models import Media, Subtitle, SyncStatus
from jsm.providers.base import SubtitleCandidate, SubtitleProvider
from jsm.scanner.filesystem import Scanner
from jsm.scanner.moviehash import compute_moviehash
from jsm.subtitles.fileops import next_free_path, safe_write_subtitle, subtitle_destination
from jsm.subtitles.language import normalize_language
from jsm.subtitles.matcher import guess_media, rank_candidates


@dataclass
class DownloadOutcome:
    media_id: int
    language: str
    success: bool
    message: str
    subtitle_path: str | None = None
    confidence: float = 0.0
    dry_run: bool = False


class NoMatchError(Exception):
    pass


class Downloader:
    def __init__(
        self,
        db: Database,
        provider: SubtitleProvider,
        scanner: Scanner,
        settings: Settings,
    ):
        self.db = db
        self.provider = provider
        self.scanner = scanner
        self.settings = settings

    async def ensure_hash(self, media: Media) -> str | None:
        if media.hash:
            return media.hash
        try:
            # 128 KiB of file I/O - keep it off the event loop (slow NAS reads
            # would otherwise stall the TUI for every queued file).
            hash_ = await asyncio.to_thread(compute_moviehash, media.path)
        except OSError:
            return None
        if hash_ and media.id is not None:
            self.db.set_media_hash(media.id, hash_)
            media.hash = hash_
        return hash_

    async def find_best(
        self,
        media: Media,
        language: str,
        query: str | None = None,
        year: int | None = None,
    ) -> SubtitleCandidate | None:
        """Search the provider and return the best-ranked candidate.

        *query*/*year* override the guessit-derived title/year (manual search).
        """
        moviehash = await self.ensure_hash(media)
        guess = guess_media(media.filename)
        candidates = await self.provider.search(
            languages=[language],
            moviehash=moviehash,
            query=query or guess.title or Path(media.path).stem,
            year=year if year is not None else guess.year,
        )
        ranked = rank_candidates(media.filename, candidates, language=language)
        return ranked[0] if ranked else None

    async def download_for(
        self,
        media: Media,
        language: str,
        min_confidence: float = 0.0,
        dry_run: bool = False,
        query: str | None = None,
        year: int | None = None,
    ) -> DownloadOutcome:
        assert media.id is not None
        language = normalize_language(language) or language
        best = await self.find_best(media, language, query=query, year=year)
        if best is None:
            return DownloadOutcome(
                media.id, language, False, "No subtitle found", dry_run=dry_run
            )
        if best.confidence < min_confidence:
            return DownloadOutcome(
                media.id, language, False,
                f"Best match below confidence threshold "
                f"({best.confidence:.0%} < {min_confidence:.0%}: {best.match_reason})",
                confidence=best.confidence, dry_run=dry_run,
            )

        dest = next_free_path(
            subtitle_destination(Path(media.path), language, best.extension)
        )
        if dry_run:
            return DownloadOutcome(
                media.id, language, True,
                f"[dry-run] Would download '{best.release_name}' "
                f"({best.confidence:.0%}, {best.match_reason}) -> {dest.name}",
                subtitle_path=str(dest), confidence=best.confidence, dry_run=True,
            )

        content = await self.provider.download(best)
        written = safe_write_subtitle(dest, content)

        self.db.add_subtitle(
            Subtitle(
                id=None, media_id=media.id, language=language, path=str(written),
                source="downloaded", hearing_impaired=best.hearing_impaired,
                forced=best.forced, downloaded_date=_now(),
                sync_status=SyncStatus.UNKNOWN,
            )
        )
        # Re-scan the file so external-subtitle discovery and health status agree.
        refreshed = self.db.get_media(media.id)
        if refreshed is not None:
            self.scanner.rescan_media(refreshed)
        return DownloadOutcome(
            media.id, language, True,
            f"Downloaded '{best.release_name}' ({best.confidence:.0%}, {best.match_reason})",
            subtitle_path=str(written), confidence=best.confidence,
        )
