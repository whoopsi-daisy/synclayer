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
- **Account rotation** — multiple OpenSubtitles accounts, each with 20
  downloads per rolling 24 h; jsm always uses the account with the most
  remaining quota and parks jobs until quota refreshes when all are spent.
- **Safe by construction** — media files are never opened for writing;
  subtitle writes are atomic; existing files are never silently overwritten
  (collision-safe `movie.en.2.srt` naming, `.bak` backups before sync
  replaces a file); bulk operations require typing `DOWNLOAD ALL` and support
  `--dry-run`.
- **Scales** — incremental scanning (unchanged files are skipped), lazy
  hashing, per-folder browsing queries, indexed SQLite (WAL).

## Installation

```bash
pip install .            # core
pip install .[sync]      # + ffsubsync for subtitle synchronization
```

Requires Python 3.11+. `ffprobe` (ffmpeg) is optional but recommended —
without it, duration and embedded-subtitle detection are skipped.

## Configuration

First run creates `~/.config/jellyfin-subtitle-manager/`:

- **`config.toml`** — set your library roots, wanted languages, and your
  OpenSubtitles **API key** (required by their REST API; free at
  <https://www.opensubtitles.com/en/consumers>):

  ```toml
  libraries = ["/media", "/media2"]
  languages = ["en"]
  api_key = "YOUR_API_KEY"
  sync_by_default = false
  bulk_min_confidence = 0.99
  ```

- **`accounts.conf`** — one `username;password` per line (file is chmod 600;
  no credentials ship with the app):

  ```
  myuser;mypassword
  otheruser;otherpassword
  ```

  Add several accounts and jsm rotates between them automatically.

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
| `D` | **Download + Sync** for selection |
| `O` | Download only |
| `S` | Sync an existing subtitle |
| `M` | manual search (override title/year/language) |
| `V` | file details (streams, subtitles, match info) |
| `R` | rescan current folder |
| `B` | bulk download all missing under this folder (typed confirmation) |
| `P`/`R`/`T`, `+`/`-` | queue: pause / resume / retry, priority |
| `Ctrl+Q` | quit |

### CLI (automation)

```bash
jsm scan                          # scan configured libraries
jsm missing --format csv -o report.csv
jsm download /media/new-movies --sync
jsm download --all --dry-run      # preview a bulk run, writes nothing
jsm sync /media/movies/Alien.mkv
jsm maintain --yes                # scan → report → download missing
```

## Safety model

The program is built so it **cannot damage your media**:

1. Every filesystem write into a library folder goes through one function
   (`jsm/subtitles/fileops.py`) that refuses any path without a subtitle
   extension (`.srt` `.ass` `.ssa` `.vtt`).
2. Writes are atomic (temp file + rename) — no half-written subtitles.
3. Existing subtitles are never silently overwritten; replacements (sync)
   keep a `.bak` of the original.
4. Bulk downloads are gated behind a typed `DOWNLOAD ALL` confirmation, a
   confidence threshold (99% = hash matches only), and a dry-run mode.

## Development

```bash
pip install -e .[dev]
pytest
```
