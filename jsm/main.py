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
from jsm.subtitles.language import normalize_language

BULK_CONFIRM_PHRASE = "DOWNLOAD ALL"


def _parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="jsm",
        formatter_class=fmt,
        description=(
            "Synclayer - Jellyfin Subtitle Maintenance Manager.\n\n"
            "Run 'jsm' with no arguments for the interactive TUI (file browser,\n"
            "download queue, dashboard). The subcommands below cover scripting\n"
            "and automation. Every download follows the same pipeline:\n"
            "search -> download -> Jellyfin rename (Movie.eng.srt) -> clean -> sync\n"
            "(clean/sync steps are controlled by config.toml and per-run flags)."
        ),
        epilog=(
            "quick start:\n"
            "  jsm doctor                        check config, accounts and tools\n"
            "  jsm scan                          index your libraries (with progress)\n"
            "  jsm missing                       see what needs attention\n"
            "  jsm download /media/movies        fetch subtitles for a folder\n"
            "\n"
            "configuration lives in ~/.config/jellyfin-subtitle-manager/\n"
            "(config.toml: libraries, languages, REQUIRED OpenSubtitles api_key;\n"
            " accounts.conf: one 'username;password' per line).\n"
            "\n"
            "Use 'jsm <command> --help' for details and examples of each command."
        ),
    )
    sub = parser.add_subparsers(
        dest="command", title="commands", metavar="<command>"
    )

    scan = sub.add_parser(
        "scan",
        help="index media files into the database (fast when re-run)",
        formatter_class=fmt,
        description=(
            "Walk the given folders (default: the libraries in config.toml),\n"
            "find video files (.mkv .mp4 .avi .webm), detect their subtitles\n"
            "(external files and embedded streams via ffprobe), and record\n"
            "everything in the local database with a health status per file:\n"
            "OK / missing / wrong language / unsynced.\n\n"
            "Progress is shown as processed/total with the current file. The\n"
            "first scan probes every file and can take a while on large\n"
            "libraries; re-scans skip unchanged files and are much faster.\n"
            "Media files are only ever read, never modified."
        ),
        epilog=(
            "examples:\n"
            "  jsm scan                          scan all configured libraries\n"
            "  jsm scan /media/movies            scan one folder (recursively)"
        ),
    )
    scan.add_argument("paths", nargs="*", metavar="PATH",
                      help="folders to scan (default: the configured libraries)")

    missing = sub.add_parser(
        "missing",
        help="report files whose subtitles need attention",
        formatter_class=fmt,
        description=(
            "List every scanned file that is missing a wanted-language\n"
            "subtitle, has only wrong-language subtitles, or whose subtitle\n"
            "is flagged unsynced. Reads the database - run 'jsm scan' (or use\n"
            "--rescan) first for fresh results."
        ),
        epilog=(
            "examples:\n"
            "  jsm missing                       human-readable table\n"
            "  jsm missing --rescan              scan first, then report\n"
            "  jsm missing --format csv -o report.csv"
        ),
    )
    missing.add_argument("--format", choices=["text", "csv", "json"], default="text",
                         help="output format (default: text)")
    missing.add_argument("--output", "-o", metavar="FILE",
                         help="write the report to FILE instead of stdout")
    missing.add_argument("--rescan", action="store_true",
                         help="re-scan the libraries before reporting")

    download = sub.add_parser(
        "download",
        help="search and download subtitles for files or folders",
        formatter_class=fmt,
        description=(
            "Find the best OpenSubtitles match for each file and download it.\n"
            "Matching uses the file's moviehash first (~99%% confidence), then\n"
            "filename similarity (title/year/release group). The subtitle is\n"
            "saved next to the video as Movie.eng.srt (Jellyfin naming);\n"
            "existing files are never overwritten (Movie.eng.2.srt instead).\n\n"
            "By default the configured cleanup (subscleaner) and sync\n"
            "(ffsubsync) steps run per config.toml. Downloads consume the\n"
            "OpenSubtitles per-account quota (20/day, accounts rotate\n"
            "automatically; jobs park and resume when quota refreshes).\n\n"
            "Bulk mode (--all) targets every file with missing subtitles and\n"
            "asks you to type '%s' unless --yes or --dry-run is given."
            % BULK_CONFIRM_PHRASE
        ),
        epilog=(
            "examples:\n"
            "  jsm download /media/movies             primary language, full pipeline\n"
            "  jsm download --both /media/movies      every configured language\n"
            "  jsm download -l sv /media/movies       a specific language\n"
            "  jsm download Movie.mkv --sync          force ffsubsync afterwards\n"
            "  jsm download --all --dry-run           preview a bulk run (writes nothing)\n"
            "  jsm download --all --yes --min-confidence 0.99"
        ),
    )
    download.add_argument("paths", nargs="*", metavar="PATH",
                          help="media files or folders to fetch subtitles for")
    download.add_argument("--all", action="store_true",
                          help="target every library file with missing subtitles")
    download.add_argument("--language", "-l", metavar="LANGS",
                          help="language(s) to fetch, comma-separated ISO codes "
                               "(default: primary configured language)")
    download.add_argument("--both", action="store_true",
                          help="fetch every configured language, not just the primary")
    download.add_argument("--sync", action="store_true",
                          help="run ffsubsync on each download (even if "
                               "sync_by_default is off)")
    download.add_argument("--clean", action="store_true",
                          help="run subscleaner on each download (even if "
                               "clean_by_default is off)")
    download.add_argument("--dry-run", action="store_true",
                          help="show what would be downloaded; write nothing")
    download.add_argument("--min-confidence", type=float, default=None, metavar="X",
                          help="reject matches below confidence X (0..1; 0.99 = "
                               "hash matches only; bulk default from config)")
    download.add_argument("--yes", action="store_true",
                          help=f"skip the typed '{BULK_CONFIRM_PHRASE}' bulk confirmation")

    sync = sub.add_parser(
        "sync",
        help="re-time existing subtitles against the video (ffsubsync)",
        formatter_class=fmt,
        description=(
            "Run ffsubsync on the existing subtitle of each given file or\n"
            "folder, aligning its timing to the audio track. The original\n"
            "subtitle is kept as .bak; the file is only replaced when\n"
            "ffsubsync succeeds. Files are re-scanned first, so a subtitle\n"
            "you just added by hand is picked up. Requires ffsubsync\n"
            "(pip install 'jellyfin-subtitle-manager[sync]')."
        ),
        epilog=(
            "examples:\n"
            "  jsm sync /media/movies/Alien.mkv\n"
            "  jsm sync -l sv /media/movies"
        ),
    )
    sync.add_argument("paths", nargs="+", metavar="PATH",
                      help="media files or folders whose subtitles to synchronize")
    sync.add_argument("--language", "-l", metavar="LANG",
                      help="subtitle language to sync (default: primary configured)")

    clean = sub.add_parser(
        "clean",
        help="strip ads/spam from existing subtitles (subscleaner)",
        formatter_class=fmt,
        description=(
            "Run subscleaner on the existing subtitles of the given files or\n"
            "folders, removing advertisement and spam lines. A .bak of the\n"
            "original is kept. Requires subscleaner (pip install subscleaner)."
        ),
        epilog=(
            "examples:\n"
            "  jsm clean /media/movies --dry-run      list what would be cleaned\n"
            "  jsm clean /media/movies/Alien.mkv"
        ),
    )
    clean.add_argument("paths", nargs="+", metavar="PATH",
                       help="media files or folders whose subtitles to clean")
    clean.add_argument("--language", "-l", metavar="LANG",
                       help="subtitle language to clean (default: primary configured)")
    clean.add_argument("--dry-run", action="store_true",
                       help="list the files that would be cleaned; change nothing")

    maintain = sub.add_parser(
        "maintain",
        help="one-shot cycle: scan, report, download all missing",
        formatter_class=fmt,
        description=(
            "The unattended maintenance cycle, equivalent to:\n"
            "  jsm scan && jsm missing && jsm download --all\n"
            "Scans the configured libraries, prints the missing-subtitles\n"
            "report, then queues downloads for every file that lacks one.\n"
            "Suitable for cron with --yes (and --dry-run to rehearse)."
        ),
        epilog=(
            "examples:\n"
            "  jsm maintain --dry-run            rehearse without downloading\n"
            "  jsm maintain --both --yes         cron-friendly, all languages"
        ),
    )
    maintain.add_argument("--sync", action="store_true",
                          help="run ffsubsync on each download (even if "
                               "sync_by_default is off)")
    maintain.add_argument("--clean", action="store_true",
                          help="run subscleaner on each download (even if "
                               "clean_by_default is off)")
    maintain.add_argument("--both", action="store_true",
                          help="fetch every configured language, not just the primary")
    maintain.add_argument("--dry-run", action="store_true",
                          help="show what would be downloaded; write nothing")
    maintain.add_argument("--min-confidence", type=float, default=None, metavar="X",
                          help="reject matches below confidence X (0..1; "
                               "default: bulk_min_confidence from config)")
    maintain.add_argument("--yes", action="store_true",
                          help=f"skip the typed '{BULK_CONFIRM_PHRASE}' confirmation")

    sub.add_parser(
        "accounts",
        help="verify OpenSubtitles logins and show remaining quota",
        formatter_class=fmt,
        description=(
            "Log in with every account from accounts.conf and report whether\n"
            "the credentials (and the API key from config.toml) work, plus\n"
            "how many of each account's 20 daily downloads remain.\n"
            "Exit status: 0 all good, 1 some account failed, 2 not configured."
        ),
    )
    sub.add_parser(
        "doctor",
        help="check configuration, accounts and optional tools",
        formatter_class=fmt,
        description=(
            "Print a health check of everything jsm needs: config file and\n"
            "library paths, accounts.conf, the required OpenSubtitles API\n"
            "key, and the optional tools (ffprobe, ffsubsync, subscleaner).\n"
            "Exit status is non-zero when a fatal problem was found."
        ),
    )
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
    healthy &= line(ctx.provider.has_api_key,
                    "OpenSubtitles API key available "
                    + ("(built-in application key)"
                       if ctx.provider.uses_default_key else "(from config.toml)"),
                    "no API key - the OpenSubtitles API requires one and this "
                    "build has no built-in key; set api_key in config.toml "
                    "(free at https://www.opensubtitles.com/en/consumers)",
                    fatal=True)
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
    import shutil
    import time

    roots = [Path(p) for p in paths] if paths else ctx.settings.library_paths
    if not roots:
        print("No libraries configured and no paths given. "
              f"Edit {config.config_file()} first.", file=sys.stderr)
        raise SystemExit(2)
    # Live progress, refreshed in place on a TTY; periodic lines otherwise.
    live = sys.stdout.isatty()
    state = {"last": 0.0, "milestone": 0}

    def on_progress(stats, path) -> None:
        done = stats.processed
        if live:
            now = time.monotonic()
            # Repainting the line for every file wastes more time than the
            # scan itself on fast disks - cap redraws at ~10/s.
            if now - state["last"] < 0.1 and done != stats.total:
                return
            state["last"] = now
            width = shutil.get_terminal_size().columns
            pct = f" ({done / stats.total:.0%})" if stats.total else ""
            head = f"  [{done}/{stats.total}]{pct} {stats.remaining} left  "
            name = stats.current[: max(10, width - len(head) - 2)]
            # \033[K clears to end of line so shorter updates don't leave debris.
            print(f"\r{head}{name}\033[K", end="", flush=True)
        elif done >= state["milestone"]:
            state["milestone"] = done + 500
            print(f"  {done}/{stats.total} file(s) scanned…", flush=True)

    for root in roots:
        print(f"Scanning {root} …")
        started = time.monotonic()
        stats = ctx.scanner.scan(root, recursive=True, on_progress=on_progress)
        if live:
            print("\r\033[K", end="")
        for warning in stats.warnings:
            print(f"  warning: {warning}", file=sys.stderr)
        elapsed = time.monotonic() - started
        skipped = f", {stats.skipped} skipped" if stats.skipped else ""
        print(f"  {stats.scanned} file(s) in {stats.directories} folder(s) "
              f"({stats.added} new, {stats.changed} changed, "
              f"{stats.removed} removed{skipped}) in {elapsed:.1f}s")


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
    languages: list[str],
    sync: bool,
    dry_run: bool,
    min_confidence: float,
) -> int:
    failures = 0
    if dry_run:
        for m in media:
            for language in languages:
                try:
                    outcome = await ctx.downloader.download_for(
                        m, language, min_confidence=min_confidence, dry_run=True
                    )
                except Exception as exc:
                    print(f"err {m.filename} [{language}]: {exc}")
                    failures += 1
                    continue
                tag = f"{m.filename} [{language}]"
                print(("ok " if outcome.success else "-- ") + f"{tag}: {outcome.message}")
                failures += 0 if outcome.success else 1
        return failures

    action = JobAction.DOWNLOAD_SYNC if sync else JobAction.DOWNLOAD
    job_ids = set()
    for m in media:
        assert m.id is not None
        for language in languages:
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
    return ctx.settings.primary_language


def _languages(ctx: AppContext, override: str | None, both: bool) -> list[str]:
    """Languages to fetch: an explicit --language (comma-separated) wins;
    otherwise --both means every configured language, and the default is just
    the primary."""
    if override:
        raw = [x.strip() for x in override.split(",") if x.strip()]
    elif both:
        raw = list(ctx.settings.languages)
    else:
        raw = [ctx.settings.primary_language]
    out: list[str] = []
    for lang in raw:
        norm = normalize_language(lang) or lang
        if norm not in out:
            out.append(norm)
    return out or ["en"]


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
    languages = _languages(ctx, args.language, args.both)
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
        ctx, _run_downloads(ctx, media, languages, sync, args.dry_run, min_confidence)
    ))
    return 1 if failures else 0


def cmd_clean(ctx: AppContext, args: argparse.Namespace) -> int:
    from jsm.subtitles.cleaner import subscleaner_available

    if not subscleaner_available():
        print("subscleaner is not installed. Install it with: pip install subscleaner",
              file=sys.stderr)
        return 2
    language = normalize_language(_language(ctx, args.language)) or _language(ctx, args.language)
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
    if not ctx.provider.has_api_key:
        print("No OpenSubtitles API key available - every login will fail.\n"
              "The OpenSubtitles REST API requires an API key for ALL requests "
              "(username/password alone is not enough), and this build ships "
              "without a built-in application key.\n"
              "Create a free key at https://www.opensubtitles.com/en/consumers "
              f"and set api_key in {config.config_file()}", file=sys.stderr)
        return 2
    print(f"Checking {len(usernames)} OpenSubtitles account(s)...")

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
    languages = _languages(ctx, None, args.both)
    failures = asyncio.run(_in_one_loop(
        ctx, _run_downloads(ctx, media, languages, sync, args.dry_run, min_confidence)
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
        try:
            return _dispatch(ctx, args)
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130
    finally:
        asyncio.run(ctx.close())


def _dispatch(ctx: AppContext, args: argparse.Namespace) -> int:
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


if __name__ == "__main__":
    raise SystemExit(main())
