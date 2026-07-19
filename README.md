# Synclayer — Jellyfin Subtitle Maintenance Manager

A Linux-first **terminal application** for keeping subtitles healthy across
large Jellyfin libraries (10,000+ files). Browse your media folders like a
file manager, see subtitle health at a glance, select the movies you care
about, and download — and optionally **ffsubsync-synchronize** — the best
matching subtitles in one keystroke.

Think *Radarr/Sonarr, but for subtitles — with you in the driver's seat.*

```
 Movie Title                   Status          Subtitles
 ─────────────────────────────────────────────────────────
 Alien (1979)                  ✓ OK            en
 Winnie The Pooh (2011)        ✗ Missing       -
 DuckTales (1990)              ⚠ Unsynced      en
 The Matrix (1999)             ≠ Wrong lang    de
```

## Features

- **Interactive Textual TUI** — dashboard, file browser, live download queue,
  per-file details. Fully keyboard driven.
- **Accurate matching** — OpenSubtitles moviehash first (~99% confidence),
  guessit-based filename matching (title/year/release group/resolution) as
  fallback, manual search override for tricky files.
- **Optional synchronization** — ffsubsync is off by default (CPU intensive);
  choose *Download+Sync* or *Download only* per action, or flip the global
  default.
- **Username/password login** — authentication uses the accounts in
  `accounts.conf`; an OpenSubtitles API key is *optional*. Multiple accounts
  rotate automatically (20 downloads per rolling 24 h each), and jobs park
  until quota refreshes when all are spent. `jsm accounts` validates them.
- **Jellyfin-native filenames** — downloaded subtitles are named from the
  local video basename plus the ISO 639-2/B language code
  (`Movie.mp4 → Movie.eng.srt`). Provider filenames are never used.
- **Automatic cleanup** — optionally run [subscleaner](https://pypi.org/project/subscleaner/)
  on downloaded subtitles to strip ads/spam lines (`--clean`, or `jsm clean`).
- **Graceful under pressure** — rate limits (HTTP 429) are honored with
  back-off, server errors and network hiccups are retried, and quota
  exhaustion parks jobs instead of failing them.
- **Safe by construction** — media files are never opened for writing;
  subtitle writes are atomic; existing files are never silently overwritten
  (collision-safe `Movie.eng.2.srt` naming, `.bak` backups before sync/clean
  rewrites a file); bulk operations require typing `DOWNLOAD ALL` and support
  `--dry-run`.
- **Scales** — incremental scanning with live progress, lazy hashing,
  per-folder browsing queries, indexed SQLite (WAL).

## Installation

One command, safe to re-run, and it **reuses anything already installed** on
your system (ffmpeg, ffsubsync, distro-packaged Python libraries):

```bash
./install.sh              # core
./install.sh --with-sync  # + ffsubsync for subtitle synchronization
```

Optional cleanup support (subscleaner) can be added any time with
`pip install subscleaner` or `pip install .[clean]` (`.[all]` for both).

The script checks for Python 3.11+, installs jsm into a private virtualenv
(created with `--system-site-packages`, so existing Python dependencies are
reused and nothing on your system is modified), links `jsm` into
`~/.local/bin`, detects already-present `ffprobe`/`ffsubsync` instead of
reinstalling them, and finishes with a health check.

Prefer doing it yourself? Any of these works too:

```bash
pipx install .[sync]                  # if you use pipx
pip install --user .[sync]            # classic user install
python3 -m venv v && v/bin/pip install .[sync]
```

`ffprobe` (ffmpeg) is optional but recommended — without it, duration and
embedded-subtitle detection are skipped. Check your setup any time with:

```bash
jsm doctor
```

## Configuration

First run creates `~/.config/jellyfin-subtitle-manager/`:

- **`accounts.conf`** — the primary credential. One `username;password` per
  line (file is chmod 600; no credentials ship with the app):

  ```
  myuser;mypassword
  otheruser;otherpassword
  ```

  Add several accounts and jsm rotates between them automatically. Validate
  them with `jsm accounts`.

- **`config.toml`** — library roots, wanted languages, and optional settings.
  The OpenSubtitles **API key is optional** — set it only if your account
  requires one (free at <https://www.opensubtitles.com/en/consumers>):

  ```toml
  libraries = ["/media", "/media2"]
  languages = ["en"]        # ISO 639-1; output files use 639-2/B (eng, ...)
  api_key = ""              # optional
  sync_by_default = false   # run ffsubsync after every download
  clean_by_default = true   # run subscleaner after every download
  bulk_min_confidence = 0.99
  ```

### Languages: primary + secondary

`languages` is a priority list. The **first** entry is your primary/default
language — a normal download fetches just that. Extra entries are secondary
languages, fetched only when you ask for **both** (the `G` key in the browser,
or `--both` on the CLI).

```toml
languages = ["en"]         # English only
languages = ["en", "sv"]   # English primary, Swedish secondary (default)
```

Downloaded files are Jellyfin-named per language: `Movie.eng.srt`, `Movie.swe.srt`.

### What a download does (by default)

Out of the box, downloading a subtitle runs the **full pipeline** —
**download → rename (Jellyfin `Movie.eng.srt`) → clean (subscleaner) → sync
(ffsubsync)**. Cleaning is skipped harmlessly if subscleaner isn't installed;
same for sync/ffsubsync. Turn either off globally with `sync_by_default` /
`clean_by_default`, or per-run on the CLI.

## Usage

### Interactive TUI (the main mode)

```bash
jsm
```

| Key | Action |
| --- | --- |
| `1` / `2` / `3` | Dashboard / Browser / Queue |
| arrows, `Enter` | navigate the folder tree |
| `Space` | select/deselect a file (multi-select) |
| `F` `L` `U` `A` | filter: missing / wrong language / unsynced / all |
| `D` | **Download** primary language (clean + sync included) |
| `G` | **Get both** — download every configured language |
| `O` | Download only (skip sync) |
| `S` | Sync an existing subtitle |
| `M` | manual search (override title/year/language) |
| `V` | file details (streams, subtitles, match info) |
| `R` | rescan current folder |
| `B` | bulk download all missing under this folder (typed confirmation) |
| `P`/`R`/`T`, `+`/`-` | queue: pause / resume / retry, priority |
| `Ctrl+Q` | quit |

### CLI (automation)

```bash
jsm scan                          # scan configured libraries (live progress)
jsm accounts                      # validate OpenSubtitles logins + show quota
jsm doctor                        # check environment and configuration
jsm missing --format csv -o report.csv
jsm download /media/new-movies    # primary language, clean + sync by default
jsm download /media/movies --both # every configured language (en + sv)
jsm download -l sv /media/movies  # a specific language (or -l en,sv)
jsm download --all --dry-run      # preview a bulk run, writes nothing
jsm sync  /media/movies/Alien.mkv # ffsubsync existing subtitles
jsm clean /media/movies --dry-run # subscleaner ad/spam removal
jsm maintain --both --yes         # scan → report → download missing (all langs)
```

## Safety model

The program is built so it **cannot damage your media**:

1. Every filesystem write into a library folder goes through one function
   (`jsm/subtitles/fileops.py`) that refuses any path without a subtitle
   extension (`.srt` `.ass` `.ssa` `.vtt`).
2. Writes are atomic (temp file + rename) — no half-written subtitles.
3. Existing subtitles are never silently overwritten; replacements (sync and
   clean) keep a `.bak` of the original, and cleanup runs on a temp copy so
   the original is only replaced on success.
4. Bulk downloads are gated behind a typed `DOWNLOAD ALL` confirmation, a
   confidence threshold (99% = hash matches only), and a dry-run mode.

## Development

```bash
pip install -e .[dev]
pytest
```
