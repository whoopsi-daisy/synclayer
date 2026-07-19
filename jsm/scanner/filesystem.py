"""Filesystem scanner.

Strictly read-only with respect to the library: it stats, lists and (via
ffprobe) inspects files - it never writes into media folders.

Scanning is incremental: a media file whose size and mtime are unchanged is not
re-probed; external subtitle discovery is re-done on every scan because it is a
cheap directory listing.

Large-library design (10k+ files):

- The tree is walked first (cheap directory listings) so the total file count
  is known up front and progress can be reported as *processed/total* with the
  current filename.
- ffprobe runs are the dominant cost on a first scan (one subprocess per new
  or changed file). They are submitted to a small thread pool for the whole
  tree at once, so probes overlap regardless of how files are spread across
  directories (one-movie-per-folder layouts gain the most).
- A single unreadable or oddly named file must never abort the scan: per-file
  failures are recorded as warnings (capped) and the scan continues. Paths
  that are not valid UTF-8 (surrogate escapes) cannot be stored in SQLite and
  are skipped with a warning instead of crashing.
"""

from __future__ import annotations

import os
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar

from jsm.database.db import Database, _now
from jsm.database.models import Media, MediaStatus, Subtitle, SyncStatus
from jsm.scanner import ffprobe
from jsm.subtitles.language import normalize_language, parse_subtitle_filename

from jsm.subtitles.fileops import SUBTITLE_EXTENSIONS

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm"}

# ffprobe is subprocess-bound; a few in flight keeps the CPU busy without
# stampeding a NAS with parallel reads.
PROBE_WORKERS = 4

# Called after each media file with (running stats, file just processed).
ProgressCallback = Callable[["ScanStats", Path], None]


@dataclass
class ScanStats:
    scanned: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    skipped: int = 0
    directories: int = 0
    total: int = 0  # media files found by the initial walk (0 while unknown)
    processed: int = 0  # files handled so far, out of *total*
    current: str = ""  # filename being processed, for progress displays
    warnings: list[str] = field(default_factory=list)

    MAX_WARNINGS: ClassVar[int] = 50

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.processed)

    def warn(self, message: str) -> None:
        """Record a warning, capped so a broken share cannot flood memory."""
        if len(self.warnings) < self.MAX_WARNINGS:
            self.warnings.append(message)
        elif len(self.warnings) == self.MAX_WARNINGS:
            self.warnings.append(
                f"(more warnings suppressed after the first {self.MAX_WARNINGS})"
            )


def _utf8_safe(path: str) -> bool:
    """SQLite only stores valid UTF-8; paths with surrogate escapes cannot be
    persisted and must be skipped instead of crashing the scan."""
    try:
        path.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


@dataclass
class _DirWork:
    """One directory's worth of scan work, gathered by the walk phase."""

    directory: Path
    media: list[tuple[Path, os.stat_result, Media | None, bool]]  # (path, stat, existing, changed)
    sub_files: list[Path]
    seen_paths: set[str]


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
        ScanStats and the current file after each media file is processed -
        used by the CLI and TUI to show live progress for large libraries."""
        stats = ScanStats()
        root = Path(root)
        if not root.is_dir():
            stats.warn(f"Not a directory: {root}")
            return stats

        # Phase 1: walk the tree. Directory listings are cheap, so this gives
        # an accurate total (for progress) before the expensive work starts.
        work = self._walk(root, recursive, stats)
        stats.total = sum(len(w.media) for w in work)

        # Phase 2: probe new/changed files in parallel, then persist per
        # directory in walk order.
        executor: ThreadPoolExecutor | None = None
        futures: dict[str, Future] = {}
        try:
            if ffprobe.ffprobe_available():
                executor = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
                for w in work:
                    for path, _, _, is_changed in w.media:
                        if is_changed:
                            futures[str(path)] = executor.submit(ffprobe.probe, path)
            for w in work:
                self._process_dir(w, futures, stats, on_progress)
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        return stats

    def rescan_media(self, media: Media) -> Media:
        """Re-check a single file (used after downloads/sync)."""
        path = Path(media.path)
        if not path.exists():
            return media
        stat = path.stat()
        existing = self.db.get_media_by_path(str(path))
        changed = self._is_changed(existing, stat)
        probe_result = (
            ffprobe.probe(path)
            if changed and ffprobe.ffprobe_available() else None
        )
        siblings = self._subtitle_files_in(path.parent)
        media = self._process_media(
            path, stat, existing, changed, probe_result, siblings, ScanStats()
        )
        self.db.conn.commit()
        return media

    # ----------------------------------------------------------------- internal

    @staticmethod
    def _is_changed(existing: Media | None, stat: os.stat_result) -> bool:
        return (
            existing is None
            or existing.size != stat.st_size
            or abs(existing.mtime - stat.st_mtime) > 1e-6
        )

    def _walk(self, root: Path, recursive: bool, stats: ScanStats) -> list[_DirWork]:
        """Gather per-directory work lists without doing any expensive I/O."""
        result: list[_DirWork] = []
        pending = [root]
        while pending:
            directory = pending.pop()
            if not _utf8_safe(str(directory)):
                # Nothing under this folder can be stored in the database
                # (every child path inherits the bad bytes) - skip it whole.
                stats.skipped += 1
                stats.warn(
                    "Skipped folder with non-UTF-8 name: "
                    + str(directory).encode("utf-8", "replace").decode()
                )
                continue
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                stats.warn(f"Cannot read {directory}: {exc}")
                continue
            stats.directories += 1

            media_entries: list[os.DirEntry] = []
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
                        media_entries.append(entry)
                    elif suffix in SUBTITLE_EXTENSIONS:
                        if _utf8_safe(entry.path):
                            sub_files.append(Path(entry.path))
                        else:
                            stats.skipped += 1
                            stats.warn(
                                "Skipped subtitle with non-UTF-8 filename: "
                                + entry.path.encode("utf-8", "replace").decode()
                            )
                except OSError:
                    continue

            media: list[tuple[Path, os.stat_result, Media | None, bool]] = []
            seen_paths: set[str] = set()
            for entry in media_entries:
                if not _utf8_safe(entry.path):
                    stats.skipped += 1
                    stats.warn(
                        "Skipped media with non-UTF-8 filename: "
                        + entry.path.encode("utf-8", "replace").decode()
                    )
                    continue
                try:
                    stat = entry.stat()
                except OSError as exc:
                    stats.warn(f"Cannot stat {entry.path}: {exc}")
                    # A transient stat failure (flaky NFS/SMB) must not delete
                    # the file's database history - treat it as still present.
                    seen_paths.add(entry.path)
                    continue
                existing = self.db.get_media_by_path(entry.path)
                media.append(
                    (Path(entry.path), stat, existing, self._is_changed(existing, stat))
                )
                seen_paths.add(entry.path)

            result.append(_DirWork(directory, media, sub_files, seen_paths))
            if recursive:
                pending.extend(sorted(subdirs, reverse=True))  # pop() -> in order
        return result

    def _process_dir(
        self,
        w: _DirWork,
        futures: dict[str, Future],
        stats: ScanStats,
        on_progress: "ProgressCallback | None",
    ) -> None:
        any_changed = any(is_changed for (_, _, _, is_changed) in w.media)
        if any_changed and not ffprobe.ffprobe_available() and not self._warned_no_ffprobe:
            stats.warn(
                "ffprobe not found - duration and embedded subtitles "
                "are not detected"
            )
            self._warned_no_ffprobe = True

        for path, stat, existing, is_changed in w.media:
            stats.current = path.name
            future = futures.get(str(path))
            probe_result = None
            if future is not None:
                try:
                    probe_result = future.result()
                except Exception as exc:  # a bad probe must not kill the scan
                    stats.warn(f"ffprobe failed on {path.name}: {exc}")
            try:
                self._process_media(
                    path, stat, existing, is_changed, probe_result,
                    w.sub_files, stats,
                )
                stats.scanned += 1
            except Exception as exc:  # one bad file must not abort the scan
                stats.skipped += 1
                stats.warn(f"Failed to process {path.name}: {exc}")
            stats.processed += 1
            if on_progress is not None:
                on_progress(stats, path)

        stats.removed += self.db.delete_media_not_in(
            str(w.directory), w.seen_paths, commit=False
        )
        # _process_media defers commits; flush the whole directory in one go
        # instead of ~2 fsyncs per file.
        self.db.conn.commit()

    @staticmethod
    def _subtitle_files_in(directory: Path) -> list[Path]:
        try:
            return [
                p for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in SUBTITLE_EXTENSIONS
                and _utf8_safe(str(p))
            ]
        except OSError:
            return []

    def _process_media(
        self,
        path: Path,
        stat: os.stat_result,
        existing: Media | None,
        changed: bool,
        probe_result: "ffprobe.ProbeResult | None",
        sub_files: list[Path],
        stats: ScanStats,
    ) -> Media:
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
