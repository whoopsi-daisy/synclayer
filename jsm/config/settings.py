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

# Built-in default OpenSubtitles account(s), shipped so the app works out of
# the box (paired with DEFAULT_API_KEY in the provider). Users add their own
# in accounts.conf on top of these - each extra account is a separate 20/day
# quota. Empty this list to ship without a default account.
DEFAULT_ACCOUNTS: list[tuple[str, str]] = [
    ("qiqizangusa", "Y@U]rW3QF-R]+iV"),
    ("fqopp1", "Fwqlpwq333"),
    ("jaloooo3", "Whgergg4444"),
    ("jennnyyy66662", "Weg34h34h4rrr"),
    ("helllyyy3332", "Wheer4g43g443"),
]

ACCOUNTS_TEMPLATE = """\
# OpenSubtitles.com accounts, one per line, in the form:
#
#   username;password
#
# Each account allows 20 downloads per rolling 24-hour window. Add several
# accounts and jsm automatically rotates to whichever has the most remaining
# quota. Lines starting with '#' and blank lines are ignored.
#
# This build may already ship with a built-in default account (shared by
# everyone using it, so its 20/day quota is shared too). ANYTHING you add
# here is used IN ADDITION and gives you your own private quota - recommended
# if you download a lot. 'jsm accounts' lists every account in effect.
#
# NOTE: the OpenSubtitles API also needs an application API key. This build
# may ship a built-in one; otherwise set api_key in config.toml (free at
# opensubtitles.com/en/consumers). 'jsm doctor' tells you what is in place.
#
# Example:
#   myuser;mypassword
"""

CONFIG_TEMPLATE = """\
# Synclayer / Jellyfin Subtitle Manager configuration.

# Root folders of your media libraries, e.g. ["/media", "/media2"]
libraries = []

# Subtitle languages you want, in priority order (ISO 639-1 codes). The FIRST
# entry is your primary/default language - it is what a normal download fetches.
# Add more for secondary languages; they are only fetched when you ask for
# "both" (the 'G' key in the browser, or --both on the CLI).
#   languages = ["en"]        # English only
#   languages = ["en", "sv"]  # English primary, Swedish secondary
languages = ["en", "sv"]

# OpenSubtitles API key. The OpenSubtitles REST API rejects every request
# (HTTP 403) without one, even with valid username/password accounts in
# accounts.conf. Leave empty to use the application's built-in key if this
# build ships one (like the official Jellyfin plugin does); otherwise a key
# is REQUIRED here. Keys are free: log in at opensubtitles.com, open
# https://www.opensubtitles.com/en/consumers and create an "API consumer",
# then paste the key here.
api_key = ""

# Run ffsubsync on every download by default (download -> clean -> sync).
sync_by_default = true

# Run subscleaner on each downloaded subtitle to strip ads/spam lines.
# Requires the 'subscleaner' command (pip install subscleaner); if it is not
# installed this is skipped harmlessly.
clean_by_default = true

# Minimum match confidence for bulk ("download all") operations. 0.99 means
# hash matches only.
bulk_min_confidence = 0.99

# How many download jobs may run concurrently.
queue_concurrency = 1

# Paths to the optional external tools, for when they are NOT on your $PATH
# (e.g. a self-contained install under /opt). Leave empty to auto-detect on
# $PATH and in jsm's virtualenv. Each may point at the binary itself or at the
# directory containing it. 'jsm doctor' shows what was found.
#   subscleaner_path = "/opt/rogs-subscleaner/bin/subscleaner"
#   ffsubsync_path   = "/opt/ffsubsync/bin/ffsubsync"
#   ffprobe_path     = "/usr/local/bin/ffprobe"
subscleaner_path = ""
ffsubsync_path = ""
ffprobe_path = ""
"""


@dataclass
class Settings:
    libraries: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=lambda: ["en", "sv"])
    api_key: str = ""
    sync_by_default: bool = True
    clean_by_default: bool = True
    bulk_min_confidence: float = 0.99
    subscleaner_path: str = ""
    ffsubsync_path: str = ""
    ffprobe_path: str = ""

    @property
    def primary_language(self) -> str:
        return self.languages[0] if self.languages else "en"

    @property
    def secondary_languages(self) -> list[str]:
        return self.languages[1:]
    queue_concurrency: int = 1

    @property
    def library_paths(self) -> list[Path]:
        return [Path(p).expanduser() for p in self.libraries]


def base_dir() -> Path:
    """The single Synclayer home holding config, accounts, database and logs.

    Everything lives in one folder (default ``~/.synclayer``) so the whole
    state is easy to find, back up, and hand over when reporting a problem.
    Override the whole thing with ``SYNCLAYER_HOME``. The legacy split
    ``JSM_CONFIG_DIR`` / ``JSM_DATA_DIR`` overrides are still honored (tests and
    existing installs rely on them) and can even point elsewhere individually.
    """
    override = os.environ.get("SYNCLAYER_HOME") or os.environ.get("JSM_HOME")
    if override:
        return Path(override).expanduser()
    return Path("~/.synclayer").expanduser()


def config_dir() -> Path:
    override = os.environ.get("JSM_CONFIG_DIR")
    return Path(override).expanduser() if override else base_dir()


def data_dir() -> Path:
    override = os.environ.get("JSM_DATA_DIR")
    return Path(override).expanduser() if override else base_dir()


def config_file() -> Path:
    return config_dir() / "config.toml"


def accounts_file() -> Path:
    return config_dir() / "accounts.conf"


def database_file() -> Path:
    return data_dir() / "jsm.db"


def log_dir() -> Path:
    return data_dir() / "logs"


def log_file() -> Path:
    return log_dir() / "synclayer.log"


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


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def dump_config(settings: Settings) -> str:
    """Serialize Settings back to a commented config.toml (used by the in-app
    configuration form). Regenerates the file from known fields - hand-written
    comments outside these keys are not preserved, which is the trade-off for a
    safe, machine-editable form."""
    return f"""\
# Synclayer / Jellyfin Subtitle Manager configuration.
# Managed by the in-app configuration form (Menu -> Edit configuration).

# Root folders of your media libraries.
libraries = {_toml_list(settings.libraries)}

# Subtitle languages you want, in priority order (ISO 639-1). The FIRST entry
# is your primary/default language; extra entries are fetched only with "both".
languages = {_toml_list(settings.languages)}

# OpenSubtitles application API key. Leave empty to use the built-in default
# key (free to create at https://www.opensubtitles.com/en/consumers).
api_key = {_toml_str(settings.api_key)}

# Run ffsubsync on every download by default (download -> clean -> sync).
sync_by_default = {str(settings.sync_by_default).lower()}

# Run subscleaner on each downloaded subtitle to strip ads/spam lines.
clean_by_default = {str(settings.clean_by_default).lower()}

# Minimum match confidence for bulk ("download all") operations. 0.99 = hash
# matches only.
bulk_min_confidence = {settings.bulk_min_confidence}

# How many download jobs may run concurrently.
queue_concurrency = {settings.queue_concurrency}

# Paths to optional external tools when they are not on $PATH. Empty = auto.
subscleaner_path = {_toml_str(settings.subscleaner_path)}
ffsubsync_path = {_toml_str(settings.ffsubsync_path)}
ffprobe_path = {_toml_str(settings.ffprobe_path)}
"""


def save_settings(settings: Settings) -> None:
    """Write *settings* to config.toml (creating the folder if needed)."""
    ensure_first_run_files()
    config_file().write_text(dump_config(settings), encoding="utf-8")


def load_accounts() -> list[tuple[str, str]]:
    """(username, password) pairs: the built-in default(s) first, then any the
    user added in accounts.conf. Deduplicated by username so a user can shadow
    a default by re-declaring it, and so nothing is doubled."""
    pairs: list[tuple[str, str]] = list(DEFAULT_ACCOUNTS)
    try:
        text = accounts_file().read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ";" not in line:
            continue
        username, _, password = line.partition(";")
        username, password = username.strip(), password.strip()
        if username and password:
            pairs.append((username, password))
    # Later entries win (user overrides a shipped default with the same name),
    # while preserving first-seen order.
    seen: dict[str, str] = {}
    for username, password in pairs:
        seen[username] = password
    return list(seen.items())
