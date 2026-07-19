"""CLI entry point.

Run ``jsm`` with no arguments for the interactive TUI, or use subcommands for
automation:

    jsm scan                       scan configured libraries into the database
    jsm missing [--format csv]     report files needing attention
    jsm download PATH... [--sync]  download subtitles for files/folders
    jsm download --all --dry-run   preview a bulk download
    jsm sync PATH...               ffsubsync existing subtitles
    jsm maintain                   scan -> download missing -> (sync)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from jsm.config import settings as config
from jsm.core import AppContext
from jsm.database.models import JobAction, JobStatus, Media, MediaStatus
from jsm.reports import format_report, missing_report

BULK_CONFIRM_PHRASE = "DOWNLOAD ALL"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jsm", description="Synclayer - Jellyfin Subtitle Maintenance Manager"
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="scan libraries and update the database")
    scan.add_argument("paths", nargs="*", help="folders to scan (default: configured libraries)")

    missing = sub.add_parser("missing", help="report missing/wrong-language/unsynced subtitles")
    missing.add_argument("--format", choices=["text", "csv", "json"], default="text")
    missing.add_argument("--output", "-o", help="write the report to a file")
    missing.add_argument("--rescan", action="store_true", help="scan before reporting")

    download = sub.add_parser("download", help="download subtitles")
    download.add_argument("paths", nargs="*", help="files or folders to fetch subtitles for")
    download.add_argument("--all", action="store_true",
                          help="all files with missing subtitles in the library")
    download.add_argument("--language", "-l", help="subtitle language (default: first configured)")
    download.add_argument("--sync", action="store_true", help="run ffsubsync after downloading")
    download.add_argument("--dry-run", action="store_true", help="preview without downloading")
    download.add_argument("--min-confidence", type=float, default=None,
                          help="minimum match confidence 0..1 (bulk default from config)")
    download.add_argument("--yes", action="store_true",
                          help=f"skip the typed '{BULK_CONFIRM_PHRASE}' bulk confirmation")

    sync = sub.add_parser("sync", help="synchronize existing subtitles with ffsubsync")
    sync.add_argument("paths", nargs="+", help="media files or folders")
    sync.add_argument("--language", "-l", help="subtitle language (default: first configured)")

    maintain = sub.add_parser("maintain", help="full cycle: scan, download missing, optional sync")
    maintain.add_argument("--sync", action="store_true", help="also synchronize downloads")
    maintain.add_argument("--dry-run", action="store_true")
    maintain.add_argument("--min-confidence", type=float, default=None)
    maintain.add_argument("--yes", action="store_true",
                          help=f"skip the typed '{BULK_CONFIRM_PHRASE}' confirmation")
    return parser


def _scan_paths(ctx: AppContext, paths: list[str]) -> None:
    roots = [Path(p) for p in paths] if paths else ctx.settings.library_paths
    if not roots:
        print("No libraries configured and no paths given. "
              f"Edit {config.config_file()} first.", file=sys.stderr)
        raise SystemExit(2)
    for root in roots:
        print(f"Scanning {root} …")
        stats = ctx.scanner.scan(root, recursive=True)
        for warning in stats.warnings:
            print(f"  warning: {warning}", file=sys.stderr)
        print(f"  {stats.scanned} file(s) in {stats.directories} folder(s) "
              f"({stats.added} new, {stats.changed} changed, {stats.removed} removed)")


def _collect_media(ctx: AppContext, paths: list[str], status: str | None = None) -> list[Media]:
    media: list[Media] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            ctx.scanner.scan(path, recursive=True)
            media.extend(ctx.db.media_under(str(path), status=status))
        else:
            ctx.scanner.scan(path.parent, recursive=False)
            found = ctx.db.get_media_by_path(str(path))
            if found is None:
                print(f"warning: not a known media file: {path}", file=sys.stderr)
            elif status is None or found.status == status:
                media.append(found)
    return media


def _confirm_bulk(count: int, assume_yes: bool, dry_run: bool) -> bool:
    if assume_yes or dry_run:
        return True
    print(f"About to queue downloads for {count} file(s).")
    typed = input(f"Type '{BULK_CONFIRM_PHRASE}' to continue: ").strip()
    if typed != BULK_CONFIRM_PHRASE:
        print("Aborted.")
        return False
    return True


async def _run_downloads(
    ctx: AppContext,
    media: list[Media],
    language: str,
    sync: bool,
    dry_run: bool,
    min_confidence: float,
) -> int:
    failures = 0
    if dry_run:
        for m in media:
            outcome = await ctx.downloader.download_for(
                m, language, min_confidence=min_confidence, dry_run=True
            )
            print(("ok " if outcome.success else "-- ") + f"{m.filename}: {outcome.message}")
            failures += 0 if outcome.success else 1
        return failures

    action = JobAction.DOWNLOAD_SYNC if sync else JobAction.DOWNLOAD
    job_ids = set()
    for m in media:
        assert m.id is not None
        job = ctx.worker.enqueue(m.id, action, language, min_confidence=min_confidence)
        job_ids.add(job.id)
    await ctx.worker.run_until_empty()
    for job in ctx.db.jobs():
        if job.id not in job_ids:
            continue
        if job.status == JobStatus.COMPLETED:
            print(f"ok  {Path(job.media_path or '?').name}: {job.detail or 'done'}")
        elif job.status == JobStatus.FAILED:
            failures += 1
            print(f"err {Path(job.media_path or '?').name}: {job.error_message}")
        elif job.status == JobStatus.WAITING_QUOTA:
            failures += 1
            print(f"..  {Path(job.media_path or '?').name}: waiting for quota "
                  "(job stays queued; re-run later or use the TUI)")
    return failures


def _language(ctx: AppContext, override: str | None) -> str:
    if override:
        return override
    return ctx.settings.languages[0] if ctx.settings.languages else "en"


def cmd_download(ctx: AppContext, args: argparse.Namespace) -> int:
    if not args.all and not args.paths:
        print("Give paths or use --all.", file=sys.stderr)
        return 2
    language = _language(ctx, args.language)
    if args.all:
        _scan_paths(ctx, [])
        media = ctx.db.all_media(status=MediaStatus.MISSING)
        min_confidence = (args.min_confidence if args.min_confidence is not None
                          else ctx.settings.bulk_min_confidence)
        if not media:
            print("No files with missing subtitles.")
            return 0
        if not _confirm_bulk(len(media), args.yes, args.dry_run):
            return 1
    else:
        media = _collect_media(ctx, args.paths)
        min_confidence = args.min_confidence or 0.0
        if not media:
            print("Nothing to do.")
            return 0
    sync = args.sync or ctx.settings.sync_by_default
    failures = asyncio.run(
        _run_downloads(ctx, media, language, sync, args.dry_run, min_confidence)
    )
    return 1 if failures else 0


def cmd_sync(ctx: AppContext, args: argparse.Namespace) -> int:
    language = _language(ctx, args.language)
    media = _collect_media(ctx, args.paths)
    if not media:
        print("Nothing to do.")
        return 0
    job_ids = set()
    for m in media:
        assert m.id is not None
        job = ctx.worker.enqueue(m.id, JobAction.SYNC, language)
        job_ids.add(job.id)
    asyncio.run(ctx.worker.run_until_empty())
    failures = 0
    for job in ctx.db.jobs():
        if job.id not in job_ids:
            continue
        name = Path(job.media_path or "?").name
        if job.status == JobStatus.COMPLETED:
            print(f"ok  {name}: {job.detail or 'synced'}")
        elif job.status == JobStatus.FAILED:
            failures += 1
            print(f"err {name}: {job.error_message}")
    return 1 if failures else 0


def cmd_maintain(ctx: AppContext, args: argparse.Namespace) -> int:
    _scan_paths(ctx, [])
    rows = missing_report(ctx.db)
    print(format_report(rows, "text"))
    media = ctx.db.all_media(status=MediaStatus.MISSING)
    if not media:
        return 0
    min_confidence = (args.min_confidence if args.min_confidence is not None
                      else ctx.settings.bulk_min_confidence)
    if not _confirm_bulk(len(media), args.yes, args.dry_run):
        return 1
    sync = args.sync or ctx.settings.sync_by_default
    failures = asyncio.run(
        _run_downloads(ctx, media, _language(ctx, None), sync, args.dry_run, min_confidence)
    )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command is None:
        from jsm.tui.app import run_tui

        run_tui()
        return 0

    ctx = AppContext()
    try:
        if args.command == "scan":
            _scan_paths(ctx, args.paths)
            return 0
        if args.command == "missing":
            if args.rescan:
                _scan_paths(ctx, [])
            report = format_report(missing_report(ctx.db), args.format)
            if args.output:
                Path(args.output).write_text(report, encoding="utf-8")
                print(f"Report written to {args.output}")
            else:
                print(report, end="")
            return 0
        if args.command == "download":
            return cmd_download(ctx, args)
        if args.command == "sync":
            return cmd_sync(ctx, args)
        if args.command == "maintain":
            return cmd_maintain(ctx, args)
        return 2
    finally:
        asyncio.run(ctx.close())


if __name__ == "__main__":
    raise SystemExit(main())
