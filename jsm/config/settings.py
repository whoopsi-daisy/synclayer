"""Configuration handling.

Config lives in ``~/.config/jellyfin-subtitle-manager/`` (override with
``JSM_CONFIG_DIR``); the database and logs live in
``~/.local/share/jellyfin-subtitle-manager/`` (override with ``JSM_DATA_DIR``).

``accounts.conf`` holds one ``username;password`` per line. It is created as a
commented template on first run - no credentials ship with the application.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR_NAME = "jellyfin-subtitle-manager"

ACCOUNTS_TEMPLATE = """\
# OpenSubtitles.com accounts, one per line, in the form:
#
#   username;password
#
# Each account allows 20 downloads per rolling 24-hour window. Add several
# accounts and jsm automatically rotates to whichever has the most remaining
# quota. Lines starting with '#' and blank lines are ignored.
#
# Example:
#   myuser;mypassword
"""

CONFIG_TEMPLATE = """\
# Synclayer / Jellyfin Subtitle Manager configuration.

# Root folders of your media libraries, e.g. ["/media", "/media2"]
libraries = []

# Subtitle languages you want, in priority order (ISO 639-1 codes).
languages = ["en"]

# OpenSubtitles authentication uses the username/password logins in
# accounts.conf by default - that is all you need. An application API key is
# OPTIONAL: set it here only if your account requires one. Get one free at
# https://www.opensubtitles.com/en/consumers
api_key = ""

# Run ffsubsync after every download by default. Off by default because it is
# CPU intensive; the interactive workflow lets you choose per action anyway.
sync_by_default = false

# Run subscleaner on each downloaded subtitle to strip ads/spam lines.
# Off by default; requires the 'subscleaner' command (pip install subscleaner).
clean_by_default = false

# Minimum match confidence for bulk ("download all") operations. 0.99 means
# hash matches only.
bulk_min_confidence = 0.99

# How many download jobs may run concurrently.
queue_concurrency = 1
"""


@dataclass
class Settings:
    libraries: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=lambda: ["en"])
    api_key: str = ""
    sync_by_default: bool = False
    clean_by_default: bool = False
    bulk_min_confidence: float = 0.99
    queue_concurrency: int = 1

    @property
    def library_paths(self) -> list[Path]:
        return [Path(p).expanduser() for p in self.libraries]


def config_dir() -> Path:
    override = os.environ.get("JSM_CONFIG_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(xdg).expanduser() / APP_DIR_NAME


def data_dir() -> Path:
    override = os.environ.get("JSM_DATA_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME", "~/.local/share")
    return Path(xdg).expanduser() / APP_DIR_NAME


def config_file() -> Path:
    return config_dir() / "config.toml"


def accounts_file() -> Path:
    return config_dir() / "accounts.conf"


def database_file() -> Path:
    return data_dir() / "jsm.db"


def log_dir() -> Path:
    return data_dir() / "logs"


def ensure_first_run_files() -> None:
    """Create the config directory, a default config and the accounts template."""
    cdir = config_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    data_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)

    cfg = config_file()
    if not cfg.exists():
        cfg.write_text(CONFIG_TEMPLATE, encoding="utf-8")

    acc = accounts_file()
    if not acc.exists():
        acc.write_text(ACCOUNTS_TEMPLATE, encoding="utf-8")
    # Credentials file: keep it private even if the user created it themselves.
    try:
        os.chmod(acc, 0o600)
    except OSError:
        pass


def load_settings() -> Settings:
    ensure_first_run_files()
    raw: dict = {}
    try:
        with open(config_file(), "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        raw = {}
    known = {f for f in Settings.__dataclass_fields__}
    return Settings(**{k: v for k, v in raw.items() if k in known})


def load_accounts() -> list[tuple[str, str]]:
    """Parse accounts.conf into (username, password) pairs."""
    try:
        text = accounts_file().read_text(encoding="utf-8")
    except OSError:
        return []
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ";" not in line:
            continue
        username, _, password = line.partition(";")
        username, password = username.strip(), password.strip()
        if username and password:
            pairs.append((username, password))
    return pairs
