#!/usr/bin/env bash
# Synclayer installer - safe to re-run, reuses what is already installed.
#
#   ./install.sh               install / upgrade jsm
#   ./install.sh --with-sync   also install ffsubsync (subtitle synchronization)
#   ./install.sh --with-clean  also install subscleaner (ad/spam cleanup)
#   ./install.sh --with-all    install both optional tools
#
# What it does:
#   * verifies Python >= 3.11
#   * installs into a private venv (~/.local/share/jellyfin-subtitle-manager/venv)
#     created with --system-site-packages, so Python dependencies that your
#     distro already ships (textual, httpx, guessit, ...) are reused instead of
#     re-downloaded, and nothing on your system is touched or replaced
#   * links the 'jsm' command into ~/.local/bin, plus any optional tools that
#     were installed INTO the venv, so jsm can find them at runtime
#   * detects already-installed ffmpeg/ffprobe, ffsubsync and subscleaner and
#     never reinstalls or overrides them
set -euo pipefail

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/jellyfin-subtitle-manager"
VENV="$APP_DIR/venv"
BIN_DIR="$HOME/.local/bin"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

WITH_SYNC=0
WITH_CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --with-sync)  WITH_SYNC=1 ;;
        --with-clean) WITH_CLEAN=1 ;;
        --with-all)   WITH_SYNC=1; WITH_CLEAN=1 ;;
        -h|--help)    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'unknown option: %s (see --help)\n' "$arg" >&2; exit 2 ;;
    esac
done

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

# Only pip-install an optional extra when its tool isn't ALREADY on the system.
EXTRAS=""
add_extra() { EXTRAS="${EXTRAS:+$EXTRAS,}$1"; }
if [ "$WITH_SYNC" = 1 ]; then
    if command -v ffsubsync >/dev/null; then
        say "ffsubsync already installed system-wide - reusing it"
    else
        add_extra sync
    fi
fi
if [ "$WITH_CLEAN" = 1 ]; then
    if command -v subscleaner >/dev/null; then
        say "subscleaner already installed system-wide - reusing it"
    else
        add_extra clean
    fi
fi

TARGET="$REPO_DIR"
[ -n "$EXTRAS" ] && TARGET="$REPO_DIR[$EXTRAS]"
say "Installing/upgrading jsm (pip skips dependencies you already have)"
"$VENV/bin/pip" install --quiet --upgrade "$TARGET"

# --- commands on PATH --------------------------------------------------------
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/jsm" "$BIN_DIR/jsm"
say "Linked $BIN_DIR/jsm"
# Tools installed INTO the venv aren't on PATH by default; link them next to
# jsm so 'jsm sync' / 'jsm clean' can find them (system copies win via PATH).
for tool in ffsubsync subscleaner; do
    if [ -x "$VENV/bin/$tool" ] && ! command -v "$tool" >/dev/null; then
        ln -sf "$VENV/bin/$tool" "$BIN_DIR/$tool"
        say "Linked $BIN_DIR/$tool"
    fi
done
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH. Add this to your shell profile:"
       printf '      export PATH="%s:$PATH"\n' "$BIN_DIR" ;;
esac

# --- optional native tools: detect, never install behind your back -----------
have() { command -v "$1" >/dev/null || [ -x "$VENV/bin/$1" ]; }

if have ffprobe; then
    say "ffprobe found - media analysis enabled"
else
    PKG_HINT="your package manager"
    command -v apt-get >/dev/null && PKG_HINT="sudo apt install ffmpeg"
    command -v dnf     >/dev/null && PKG_HINT="sudo dnf install ffmpeg"
    command -v pacman  >/dev/null && PKG_HINT="sudo pacman -S ffmpeg"
    command -v zypper  >/dev/null && PKG_HINT="sudo zypper install ffmpeg"
    warn "ffprobe (ffmpeg) not found - optional, but recommended: $PKG_HINT"
fi

if have ffsubsync; then
    say "ffsubsync found - subtitle synchronization enabled"
else
    warn "ffsubsync not installed - sync features stay disabled." \
         "Enable with:  ./install.sh --with-sync"
fi

if have subscleaner; then
    say "subscleaner found - subtitle cleanup enabled"
else
    warn "subscleaner not installed - cleanup features stay disabled." \
         "Enable with:  ./install.sh --with-clean"
fi

# --- first-run files + health check ------------------------------------------
say "Running health check"
"$BIN_DIR/jsm" doctor || true
echo
say "Done. Next steps:"
echo "    1. Add your OpenSubtitles accounts to accounts.conf (username;password)"
echo "    2. Edit ~/.config/jellyfin-subtitle-manager/config.toml (libraries)"
echo "    3. Run: jsm"
