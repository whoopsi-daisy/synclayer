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
    download.add_argument("--clean", action="store_true",
                          help="run subscleaner on each downloaded subtitle")
    download.add_argument("--dry-run", action="store_true", help="preview without downloading")
    download.add_argument("--min-confidence", type=float, default=None,
                          help="minimum match confidence 0..1 (bulk default from config)")
    download.add_argument("--yes", action="store_true",
                          help=f"skip the typed '{BULK_CONFIRM_PHRASE}' bulk confirmation")

    sync = sub.add_parser("sync", help="synchronize existing subtitles with ffsubsync")
    sync.add_argument("paths", nargs="+", help="media files or folders")
    sync.add_argument("--language", "-l", help="subtitle language (default: first configured)")

    clean = sub.add_parser("clean", help="clean ads/spam from existing subtitles (subscleaner)")
    clean.add_argument("paths", nargs="+", help="media files or folders")
    clean.add_argument("--language", "-l", help="subtitle language (default: first configured)")
    clean.add_argument("--dry-run", action="store_true", help="list what would be cleaned")

    maintain = sub.add_parser("maintain", help="full cycle: scan, download missing, optional sync")
    maintain.add_argument("--sync", action="store_true", help="also synchronize downloads")
    maintain.add_argument("--clean", action="store_true", help="also clean downloaded subtitles")
    maintain.add_argument("--dry-run", action="store_true")
    maintain.add_argument("--min-confidence", type=float, default=None)
    maintain.add_argument("--yes", action="store_true",
                          help=f"skip the typed '{BULK_CONFIRM_PHRASE}' confirmation")

    sub.add_parser("accounts", help="validate OpenSubtitles accounts and show quota")
    sub.add_parser("doctor", help="check the environment and configuration")
    return parser


def cmd_doctor(ctx: AppContext) -> int:
    """Environment sanity check - mirrors the dashboard's tools panel."""
    import platform

    from jsm.scanner.ffprobe import ffprobe_available
    from jsm.subtitles.synchronizer import ffsubsync_available

    def line(ok: bool, good: str, bad: str, fatal: bool = False) -> bool:
        print(("  ok  " if ok else ("  ERR " if fatal else "  --  "))
              + (good if ok else bad))
        return ok or not fatal

    print(f"Synclayer doctor (Python {platform.python_version()})")
    print(f"  config: {config.config_file()}")
    print(f"  data:   {ctx.db_path}")
    healthy = True
    healthy &= line(bool(ctx.settings.libraries),
                    f"libraries configured: {', '.join(ctx.settings.libraries)}",
                    "no libraries configured - set 'libraries' in config.toml",
                    fatal=True)
    for path in ctx.settings.library_paths:
        healthy &= line(path.is_dir(), f"library exists: {path}",
                        f"library path not found: {path}", fatal=True)
    accounts = ctx.accounts.usernames
    healthy &= line(bool(accounts),
                    f"{len(accounts)} OpenSubtitles account(s) in accounts.conf "
                    "(username/password login)",
                    "no accounts in accounts.conf - downloads will fail "
                    "(add 'username;password' lines)",
                    fatal=True)
    line(bool(ctx.settings.api_key),
         "OpenSubtitles API key set (optional)",
         "no api_key set - optional, only needed if your account requires one")
    line(ffprobe_available(), "ffprobe found (media analysis enabled)",
         "ffprobe not found - install ffmpeg for duration/embedded-subtitle "
         "detection (optional)")
    line(ffsubsync_available(), "ffsubsync found (subtitle sync enabled)",
         "ffsubsync not found - sync actions disabled "
         "(pip install 'jellyfin-subtitle-manager[sync]', optional)")
    from jsm.subtitles.cleaner import subscleaner_available

    line(subscleaner_available(), "subscleaner found (subtitle cleanup enabled)",
         "subscleaner not found - cleanup disabled (pip install subscleaner, optional)")
    stats = ctx.db.media_stats()
    print(f"  info  database has {stats.get('total', 0)} media file(s) "
          f"({stats.get('missing', 0)} missing subtitles)")
    print("Everything needed for downloads is in place."
          if healthy else "Fix the ERR lines above, then re-run 'jsm doctor'.")
    return 0 if healthy else 1


def _scan_paths(ctx: AppContext, paths: list[str]) -> None:
    roots = [Path(p) for p in paths] if paths else ctx.settings.library_paths
    if not roots:
        print("No libraries configured and no paths given. "
              f"Edit {config.config_file()} first.", file=sys.stderr)
        raise SystemExit(2)
    # Live per-directory counter, refreshed in place on a TTY.
    live = sys.stdout.isatty()

    def on_progress(stats, directory) -> None:
        if live:
            print(f"\r  scanned {stats.scanned} file(s) in {stats.directories} "
                  f"folder(s)…", end="", flush=True)

    for root in roots:
        print(f"Scanning {root} …")
        stats = ctx.scanner.scan(root, recursive=True, on_progress=on_progress)
        if live:
            print("\r", end="")
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
            try:
                outcome = await ctx.downloader.download_for(
                    m, language, min_confidence=min_confidence, dry_run=True
                )
            except Exception as exc:
                print(f"err {m.filename}: {exc}")
                failures += 1
                continue
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


async def _in_one_loop(ctx: AppContext, coro) -> object:
    """Run *coro* and close the provider's HTTP client in the SAME event loop.

    The lazily-created httpx client binds to the loop it was created on;
    closing it later from main()'s final asyncio.run would target a dead loop.
    """
    try:
        return await coro
    finally:
        await ctx.provider.close()


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
    if args.clean or ctx.settings.clean_by_default:
        ctx.worker.clean_downloads = True
    failures = asyncio.run(_in_one_loop(
        ctx, _run_downloads(ctx, media, language, sync, args.dry_run, min_confidence)
    ))
    return 1 if failures else 0


def cmd_clean(ctx: AppContext, args: argparse.Namespace) -> int:
    from jsm.subtitles.cleaner import subscleaner_available

    if not subscleaner_available():
        print("subscleaner is not installed. Install it with: pip install subscleaner",
              file=sys.stderr)
        return 2
    language = _language(ctx, args.language)
    media = _collect_media(ctx, args.paths)
    if not media:
        print("Nothing to do.")
        return 0
    if args.dry_run:
        n = 0
        for m in media:
            assert m.id is not None
            subs = [s for s in ctx.db.subtitles_for(m.id)
                    if s.path and s.language in (language, "und")]
            for s in subs:
                print(f"-- would clean {Path(s.path).name}")
                n += 1
        print(f"[dry-run] {n} subtitle file(s) would be cleaned")
        return 0
    job_ids = set()
    for m in media:
        assert m.id is not None
        job = ctx.worker.enqueue(m.id, JobAction.CLEAN, language)
        job_ids.add(job.id)
    asyncio.run(_in_one_loop(ctx, ctx.worker.run_until_empty()))
    failures = 0
    for job in ctx.db.jobs():
        if job.id not in job_ids:
            continue
        name = Path(job.media_path or "?").name
        if job.status == JobStatus.COMPLETED:
            print(f"ok  {name}: {job.detail or 'cleaned'}")
        elif job.status == JobStatus.FAILED:
            failures += 1
            print(f"err {name}: {job.error_message}")
    return 1 if failures else 0


def cmd_accounts(ctx: AppContext) -> int:
    """Validate each configured account by logging in, and show quota."""
    usernames = ctx.accounts.usernames
    if not usernames:
        print("No accounts configured. Add 'username;password' lines to "
              f"{config.accounts_file()}", file=sys.stderr)
        return 2
    print(f"Checking {len(usernames)} OpenSubtitles account(s)...")
    if ctx.settings.api_key:
        print("  (API key is set and will be sent as well)")

    async def check() -> int:
        bad = 0
        try:
            for username in usernames:
                quota = ctx.accounts.quota(username)
                ok, message = await ctx.provider.validate_account(username)
                mark = "ok " if ok else "ERR"
                print(f"  {mark} {username:<20} {quota.remaining:>2}/20 downloads left"
                      + ("" if ok else f"  - {message}"))
                bad += 0 if ok else 1
        finally:
            await ctx.provider.close()
        return bad

    bad = asyncio.run(check())
    if bad:
        print(f"{bad} account(s) failed to authenticate - fix them in "
              f"{config.accounts_file()}")
    else:
        print("All accounts authenticated successfully.")
    return 1 if bad else 0


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
    asyncio.run(_in_one_loop(ctx, ctx.worker.run_until_empty()))
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
    if args.clean or ctx.settings.clean_by_default:
        ctx.worker.clean_downloads = True
    failures = asyncio.run(_in_one_loop(
        ctx, _run_downloads(ctx, media, _language(ctx, None), sync, args.dry_run, min_confidence)
    ))
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
        if args.command == "doctor":
            return cmd_doctor(ctx)
        if args.command == "accounts":
            return cmd_accounts(ctx)
        if args.command == "download":
            return cmd_download(ctx, args)
        if args.command == "sync":
            return cmd_sync(ctx, args)
        if args.command == "clean":
            return cmd_clean(ctx, args)
        if args.command == "maintain":
            return cmd_maintain(ctx, args)
        return 2
    finally:
        asyncio.run(ctx.close())


if __name__ == "__main__":
    raise SystemExit(main())
