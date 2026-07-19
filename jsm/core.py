"""Wires the pieces together for both the TUI and the CLI."""

from __future__ import annotations

from pathlib import Path

from jsm.config import settings as config
from jsm.config.settings import Settings
from jsm.database.db import Database
from jsm.providers.accounts import AccountManager
from jsm.providers.opensubtitles import OpenSubtitlesProvider
from jsm.scanner.filesystem import Scanner
from jsm.subtitles.downloader import Downloader
from jsm.subtitles.queue import QueueWorker, UpdateCallback


class AppContext:
    def __init__(
        self,
        settings: Settings | None = None,
        db_path: str | Path | None = None,
        on_job_update: UpdateCallback | None = None,
    ):
        self.settings = settings or config.load_settings()
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
                                  clean_downloads=self.settings.clean_by_default)

    def new_scanner(self) -> tuple[Database, Scanner]:
        """A scanner bound to a fresh DB connection, for use in a thread."""
        db = Database(self.db_path)
        return db, Scanner(db, self.settings.languages)

    async def close(self) -> None:
        await self.provider.close()
        self.db.close()
