"""Wires the pieces together for both the TUI and the CLI."""

from __future__ import annotations

from pathlib import Path

from jsm.activity import ActivityLog
from jsm.config import settings as config
from jsm.config.settings import Settings
from jsm.database.db import Database
from jsm.providers.accounts import AccountManager
from jsm.providers.opensubtitles import DEFAULT_API_KEY, OpenSubtitlesProvider
from jsm.scanner.filesystem import Scanner
from jsm.subtitles.downloader import Downloader
from jsm.subtitles.queue import QueueWorker, UpdateCallback
from jsm.tools import configure_tool_paths


class AppContext:
    def __init__(
        self,
        settings: Settings | None = None,
        db_path: str | Path | None = None,
        on_job_update: UpdateCallback | None = None,
    ):
        self.settings = settings or config.load_settings()
        self.activity = ActivityLog()
        self.activity.attach_file(config.log_file())
        self._apply_tool_paths()
        self.db_path = db_path if db_path is not None else config.database_file()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.db_path)
        self.scanner = Scanner(self.db, self.settings.languages)
        self.accounts = AccountManager(self.db, config.load_accounts())
        self.provider = OpenSubtitlesProvider(self.settings.api_key, self.accounts)
        self.downloader = Downloader(self.db, self.provider, self.scanner, self.settings)
        self.worker = QueueWorker(self.db, self.downloader, self.accounts,
                                  on_update=on_job_update,
                                  concurrency=self.settings.queue_concurrency,
                                  clean_downloads=self.settings.clean_by_default,
                                  activity=self.activity)
        self._log_session_start()

    def _log_session_start(self) -> None:
        """A verbose header so a shared log carries the context to diagnose it."""
        import platform

        from jsm import __version__
        from jsm.scanner.ffprobe import ffprobe_available
        from jsm.subtitles.cleaner import subscleaner_available
        from jsm.subtitles.synchronizer import ffsubsync_available

        a = self.activity
        a.trace("=" * 70)
        a.info(f"Synclayer {__version__} starting (Python {platform.python_version()}, "
               f"{platform.system()} {platform.release()})")
        a.trace(f"base dir : {config.base_dir()}")
        a.trace(f"config   : {config.config_file()}")
        a.trace(f"database : {self.db_path}")
        a.trace(f"log file : {config.log_file()}")
        a.trace(f"libraries: {self.settings.libraries or '(none configured)'}")
        a.trace(f"languages: {self.settings.languages}")
        a.trace(f"accounts : {self.accounts.usernames}")
        a.trace(f"api key  : {'built-in default' if self.provider.uses_default_key else ('set' if self.provider.has_api_key else 'MISSING')}")
        a.trace(f"tools    : ffprobe={ffprobe_available()} "
                f"ffsubsync={ffsubsync_available()} subscleaner={subscleaner_available()}")
        a.trace(f"defaults : sync={self.settings.sync_by_default} "
                f"clean={self.settings.clean_by_default} "
                f"bulk_min_confidence={self.settings.bulk_min_confidence}")

    def _apply_tool_paths(self) -> None:
        # Make configured tool locations discoverable everywhere (scanner,
        # cleaner, synchronizer) before anything tries to find them.
        configure_tool_paths({
            "subscleaner": self.settings.subscleaner_path,
            "ffsubsync": self.settings.ffsubsync_path,
            "ffprobe": self.settings.ffprobe_path,
        })

    def reload_config(self) -> Settings:
        """Re-read config.toml and accounts.conf and apply them to the already
        running components, so edits made in the app take effect without a
        restart. Returns the fresh Settings."""
        self.settings = config.load_settings()
        self._apply_tool_paths()
        self.scanner.wanted_languages = self.settings.languages
        # Credentials/key may have changed - reset cached logins so the next
        # request re-authenticates with the new values.
        self.accounts._accounts = dict(config.load_accounts())
        # Push new credentials into the provider without assuming a concrete
        # implementation (tests inject fakes without these internals).
        if hasattr(self.provider, "api_key"):
            self.provider.api_key = self.settings.api_key or DEFAULT_API_KEY
        for cache in ("_tokens", "_base_urls"):
            store = getattr(self.provider, cache, None)
            if hasattr(store, "clear"):
                store.clear()
        self.downloader.settings = self.settings
        self.worker.clean_downloads = self.settings.clean_by_default
        self.worker.concurrency = max(1, self.settings.queue_concurrency)
        self.activity.info("Reloaded configuration and credentials")
        return self.settings

    def new_scanner(self) -> tuple[Database, Scanner]:
        """A scanner bound to a fresh DB connection, for use in a thread."""
        db = Database(self.db_path)
        return db, Scanner(db, self.settings.languages)

    async def close(self) -> None:
        await self.provider.close()
        self.db.close()
        self.activity.info("Synclayer stopping")
        self.activity.close()
