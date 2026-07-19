#!/usr/bin/env bash
# Synclayer installer - safe to re-run, reuses what is already installed.
#
#   ./install.sh              install / upgrade jsm
#   ./install.sh --with-sync  also install ffsubsync for subtitle syncing
#
# What it does:
#   * verifies Python >= 3.11
#   * installs into a private venv (~/.local/share/jellyfin-subtitle-manager/venv)
#     created with --system-site-packages, so Python dependencies that your
#     distro already ships (textual, httpx, guessit, ...) are reused instead of
#     re-downloaded, and nothing on your system is touched or replaced
#   * links the 'jsm' command into ~/.local/bin
#   * detects already-installed ffmpeg/ffprobe and ffsubsync and skips them
set -euo pipefail

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/jellyfin-subtitle-manager"
VENV="$APP_DIR/venv"
BIN_DIR="$HOME/.local/bin"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
WITH_SYNC=0
[ "${1:-}" = "--with-sync" ] && WITH_SYNC=1

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Python ------------------------------------------------------------------
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 not found. Install Python 3.11+ first."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11+ required, found $("$PYTHON" -V 2>&1). Install a newer python3."
say "Using $("$PYTHON" -V 2>&1) at $PYTHON"

# --- venv (reuses system-wide Python packages when present) ------------------
if [ -x "$VENV/bin/python" ]; then
    say "Reusing existing environment at $VENV"
else
    say "Creating environment at $VENV (with access to system packages)"
    "$PYTHON" -m venv --system-site-packages "$VENV" \
        || die "venv creation failed. On Debian/Ubuntu: sudo apt install python3-venv"
fi

say "Installing/upgrading jsm (pip skips dependencies you already have)"
if [ "$WITH_SYNC" = 1 ] && ! command -v ffsubsync >/dev/null; then
    "$VENV/bin/pip" install --quiet --upgrade "$REPO_DIR[sync]"
else
    [ "$WITH_SYNC" = 1 ] && say "ffsubsync already installed system-wide - reusing it"
    "$VENV/bin/pip" install --quiet --upgrade "$REPO_DIR"
fi

# --- command on PATH ---------------------------------------------------------
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/jsm" "$BIN_DIR/jsm"
say "Linked $BIN_DIR/jsm"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH. Add this to your shell profile:"
       printf '      export PATH="%s:$PATH"\n' "$BIN_DIR" ;;
esac

# --- optional native tools: detect, never install behind your back -----------
if command -v ffprobe >/dev/null; then
    say "ffprobe found - media analysis enabled"
else
    PKG_HINT="your package manager"
    command -v apt-get >/dev/null && PKG_HINT="sudo apt install ffmpeg"
    command -v dnf     >/dev/null && PKG_HINT="sudo dnf install ffmpeg"
    command -v pacman  >/dev/null && PKG_HINT="sudo pacman -S ffmpeg"
    command -v zypper  >/dev/null && PKG_HINT="sudo zypper install ffmpeg"
    warn "ffprobe (ffmpeg) not found - optional, but recommended: $PKG_HINT"
fi
if command -v ffsubsync >/dev/null || [ -x "$VENV/bin/ffsubsync" ]; then
    say "ffsubsync found - subtitle synchronization enabled"
elif [ "$WITH_SYNC" = 0 ]; then
    warn "ffsubsync not installed - sync features stay disabled." \
         "Re-run with:  ./install.sh --with-sync"
fi

# --- first-run files + health check ------------------------------------------
say "Running health check"
"$BIN_DIR/jsm" doctor || true
echo
say "Done. Next steps:"
echo "    1. Edit ~/.config/jellyfin-subtitle-manager/config.toml (libraries, api_key)"
echo "    2. Add your OpenSubtitles accounts to accounts.conf (username;password)"
echo "    3. Run: jsm"
