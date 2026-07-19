"""Filesystem scanner.

Strictly read-only with respect to the library: it stats, lists and (via
ffprobe) inspects files - it never writes into media folders.

Scanning is incremental: a media file whose size and mtime are unchanged is not
re-probed; external subtitle discovery is re-done on every scan because it is a
cheap directory listing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from jsm.database.db import Database, _now
from jsm.database.models import Media, MediaStatus, Subtitle, SyncStatus
from jsm.scanner import ffprobe
from jsm.subtitles.language import normalize_language, parse_subtitle_filename

from jsm.subtitles.fileops import SUBTITLE_EXTENSIONS

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm"}

# Called after each directory with (running stats, directory just finished).
ProgressCallback = Callable[["ScanStats", Path], None]


@dataclass
class ScanStats:
    scanned: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    directories: int = 0
    warnings: list[str] = field(default_factory=list)


def compute_status(subtitles: list[Subtitle], wanted_languages: list[str]) -> str:
    """Derive the health status of a media file from its known subtitles.

    An *external* subtitle with unknown language (plain ``movie.srt``) is
    assumed to be in the user's primary language and counts as matching.
    Embedded streams without a language tag do NOT count - an untagged stream
    could be any language, so the file still needs a real wanted-language sub.
    """
    wanted = {normalize_language(lang) for lang in wanted_languages}
    wanted.discard(None)
    matching = [
        s for s in subtitles
        if s.language in wanted
        or (s.language in ("und", None) and s.source == "external")
    ]
    if matching:
        bad = (SyncStatus.UNSYNCED, SyncStatus.SYNC_FAILED)
        if all(s.sync_status in bad for s in matching):
            return MediaStatus.UNSYNCED
        return MediaStatus.OK
    if subtitles:
        return MediaStatus.WRONG_LANG
    return MediaStatus.MISSING


class Scanner:
    def __init__(self, db: Database, wanted_languages: list[str]):
        self.db = db
        self.wanted_languages = wanted_languages
        self._warned_no_ffprobe = False

    # ------------------------------------------------------------------ public

    def scan(
        self,
        root: str | Path,
        recursive: bool = True,
        on_progress: "ProgressCallback | None" = None,
    ) -> ScanStats:
        """Scan *root*. If *on_progress* is given it is called with the running
        ScanStats and the current directory after each folder is processed -
        used by the CLI to print live progress for large libraries."""
        stats = ScanStats()
        root = Path(root)
        if not root.is_dir():
            stats.warnings.append(f"Not a directory: {root}")
            return stats
        self._scan_dir(root, recursive, stats, on_progress)
        return stats

    def rescan_media(self, media: Media) -> Media:
        """Re-check a single file (used after downloads/sync)."""
        path = Path(media.path)
        if not path.exists():
            return media
        siblings = self._subtitle_files_in(path.parent)
        media = self._process_media(path, path.stat(), siblings, ScanStats())
        self.db.conn.commit()
        return media

    # ----------------------------------------------------------------- internal

    def _scan_dir(
        self,
        directory: Path,
        recursive: bool,
        stats: ScanStats,
        on_progress: "ProgressCallback | None" = None,
    ) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            stats.warnings.append(f"Cannot read {directory}: {exc}")
            return
        stats.directories += 1

        media_files: list[os.DirEntry] = []
        sub_files: list[Path] = []
        subdirs: list[Path] = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not entry.name.startswith("."):
                        subdirs.append(Path(entry.path))
                    continue
                suffix = Path(entry.name).suffix.lower()
                if suffix in MEDIA_EXTENSIONS:
                    media_files.append(entry)
                elif suffix in SUBTITLE_EXTENSIONS:
                    sub_files.append(Path(entry.path))
            except OSError:
                continue

        seen_paths: set[str] = set()
        for entry in media_files:
            try:
                stat = entry.stat()
            except OSError as exc:
                stats.warnings.append(f"Cannot stat {entry.path}: {exc}")
                # A transient stat failure (flaky NFS/SMB) must not delete the
                # file's database history - treat it as still present.
                seen_paths.add(entry.path)
                continue
            self._process_media(Path(entry.path), stat, sub_files, stats)
            seen_paths.add(entry.path)
            stats.scanned += 1

        stats.removed += self.db.delete_media_not_in(
            str(directory), seen_paths, commit=False
        )
        # _process_media defers commits; flush the whole directory in one go
        # instead of ~2 fsyncs per file.
        self.db.conn.commit()

        if on_progress is not None:
            on_progress(stats, directory)

        if recursive:
            for subdir in sorted(subdirs):
                self._scan_dir(subdir, recursive, stats, on_progress)

    @staticmethod
    def _subtitle_files_in(directory: Path) -> list[Path]:
        try:
            return [
                p for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in SUBTITLE_EXTENSIONS
            ]
        except OSError:
            return []

    def _process_media(
        self,
        path: Path,
        stat: os.stat_result,
        sub_files: list[Path],
        stats: ScanStats,
    ) -> Media:
        existing = self.db.get_media_by_path(str(path))
        changed = (
            existing is None
            or existing.size != stat.st_size
            or abs(existing.mtime - stat.st_mtime) > 1e-6
        )

        media = existing or Media(
            id=None, path=str(path), filename=path.name,
            directory=str(path.parent), size=stat.st_size, mtime=stat.st_mtime,
        )
        media.size = stat.st_size
        media.mtime = stat.st_mtime
        media.scan_date = _now()

        embedded: list[Subtitle] = []
        if changed:
            media.hash = None  # file content changed - hash must be recomputed
            probe_result = ffprobe.probe(path) if ffprobe.ffprobe_available() else None
            if probe_result is None and not ffprobe.ffprobe_available():
                if not self._warned_no_ffprobe:
                    stats.warnings.append(
                        "ffprobe not found - duration and embedded subtitles "
                        "are not detected"
                    )
                    self._warned_no_ffprobe = True
            if probe_result is not None:
                media.duration = probe_result.duration
                for emb in probe_result.embedded_subtitles:
                    embedded.append(
                        Subtitle(
                            id=None, media_id=0,
                            language=normalize_language(emb.language) or "und",
                            path=None, source="embedded",
                            forced=emb.forced, hearing_impaired=emb.hearing_impaired,
                        )
                    )
            if existing is None:
                stats.added += 1
            else:
                stats.changed += 1
        elif existing is not None and existing.id is not None:
            # unchanged file: keep previously recorded embedded streams
            embedded = [
                s for s in self.db.subtitles_for(existing.id) if s.source == "embedded"
            ]

        external = self._match_external_subtitles(path, sub_files)
        subs = external + embedded
        media.status = compute_status(subs, self.wanted_languages)
        media = self.db.upsert_media(media, commit=False)
        assert media.id is not None
        for sub in subs:
            sub.media_id = media.id
        self.db.replace_subtitles(media.id, subs, commit=False)
        return media

    @staticmethod
    def _match_external_subtitles(media_path: Path, sub_files: list[Path]) -> list[Subtitle]:
        """Match sub files in the same directory whose stem starts with the
        media stem (``Movie (2010).en.srt`` next to ``Movie (2010).mkv``)."""
        stem = media_path.stem
        matched: list[Subtitle] = []
        for sub in sub_files:
            sub_stem = sub.stem
            if not (sub_stem == stem or sub_stem.startswith(stem + ".")):
                continue
            language, forced, hi = parse_subtitle_filename(sub, media_stem=stem)
            matched.append(
                Subtitle(
                    id=None, media_id=0, language=language or "und",
                    path=str(sub), source="external", forced=forced,
                    hearing_impaired=hi,
                )
            )
        return matched
