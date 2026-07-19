"""Locating optional external tools (ffprobe, ffsubsync, subscleaner).

Resolution order for a tool ``name``:

1. An explicit path configured for it - from ``config.toml``
   (``subscleaner_path`` / ``ffsubsync_path`` / ``ffprobe_path``, wired in by
   :class:`jsm.core.AppContext`) or the ``JSM_<NAME>_PATH`` environment
   variable. The value may be the binary itself or a directory containing it,
   so ``/opt/rogs-subscleaner/bin/subscleaner`` and ``/opt/rogs-subscleaner/bin``
   both work.
2. ``$PATH`` (``shutil.which``).
3. Next to the running interpreter - when jsm is installed into a private
   virtualenv and launched through a symlink, pip-installed console scripts
   (``ffsubsync``, ``subscleaner``) land there but are not on ``$PATH``.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Explicit tool paths registered at startup (see configure_tool_paths). Keyed
# by tool name; values are a file or a directory containing the binary.
_configured_paths: dict[str, str] = {}


def configure_tool_paths(paths: dict[str, str | None]) -> None:
    """Register explicit tool locations (called once from AppContext).

    Empty/None values are ignored so an unset config option never shadows the
    normal search.
    """
    for name, value in paths.items():
        if value:
            _configured_paths[name] = value


def _env_override(name: str) -> str | None:
    # subscleaner -> JSM_SUBSCLEANER_PATH
    key = "JSM_" + name.upper().replace("-", "_") + "_PATH"
    return os.environ.get(key)


def _match_override(name: str, value: str) -> str | None:
    """Resolve a configured override that may be a file or a directory."""
    path = Path(value).expanduser()
    if path.is_dir():
        candidate = path / name
        return str(candidate) if candidate.is_file() else None
    if path.is_file():
        return str(path)
    return None


def resolve_tool(name: str, override: str | None = None) -> str | None:
    """Full path to *name*, honoring configured/env overrides first."""
    for value in (override, _configured_paths.get(name), _env_override(name)):
        if value:
            matched = _match_override(name, value)
            if matched:
                return matched
    found = shutil.which(name)
    if found:
        return found
    candidate = Path(sys.executable).resolve().parent / name
    if candidate.is_file():
        return str(candidate)
    return None


def tool_available(name: str, override: str | None = None) -> bool:
    return resolve_tool(name, override) is not None
