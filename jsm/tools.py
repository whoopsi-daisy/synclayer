"""Locating optional external tools (ffprobe, ffsubsync, subscleaner).

``shutil.which`` only searches ``$PATH``. When jsm is installed into a private
virtualenv (as install.sh does) and launched through a symlink, tools that were
pip-installed into that same venv are NOT on ``$PATH``. So we also look next to
the running interpreter (the venv's ``bin`` directory), which is where console
scripts like ``ffsubsync`` and ``subscleaner`` land.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def resolve_tool(name: str) -> str | None:
    """Full path to *name*, searching $PATH then the interpreter's bin dir."""
    found = shutil.which(name)
    if found:
        return found
    candidate = Path(sys.executable).resolve().parent / name
    if candidate.is_file():
        return str(candidate)
    return None


def tool_available(name: str) -> bool:
    return resolve_tool(name) is not None
