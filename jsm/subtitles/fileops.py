"""The single chokepoint for writing into media library folders.

Safety contract (the whole application relies on this module):

* Only files with a subtitle extension may ever be written or replaced.
  Attempting to write to anything else raises :class:`UnsafeWriteError`.
* Media files are never opened for writing anywhere in the codebase.
* Writes are atomic: content goes to a temp file in the destination directory
  and is moved into place with ``os.replace``.
* An existing file is never silently overwritten: either the caller asks for a
  non-colliding name, or ``overwrite=True`` is passed and the original is
  first copied to a non-colliding ``.bak`` file.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}


class UnsafeWriteError(Exception):
    """Raised when a write would target a non-subtitle file."""


def _ensure_subtitle_path(path: Path) -> None:
    if path.suffix.lower() not in SUBTITLE_EXTENSIONS:
        raise UnsafeWriteError(
            f"Refusing to write non-subtitle file: {path} "
            f"(allowed extensions: {sorted(SUBTITLE_EXTENSIONS)})"
        )


def next_free_path(path: Path) -> Path:
    """Return *path* if free, else ``stem.2.ext``, ``stem.3.ext``, ..."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem}.{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"No free filename variant for {path}")


def backup_path_for(path: Path) -> Path:
    candidate = path.with_name(path.name + ".bak")
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak{n}")
        n += 1
    return candidate


def safe_write_subtitle(dest: Path, data: bytes, overwrite: bool = False) -> Path:
    """Atomically write subtitle *data* to *dest*.

    Returns the path actually written (== *dest*). If *dest* exists and
    ``overwrite`` is False a :class:`FileExistsError` is raised - callers that
    want a sibling name must use :func:`next_free_path` first. With
    ``overwrite=True`` the existing file is copied to a ``.bak`` first.
    """
    dest = Path(dest)
    _ensure_subtitle_path(dest)
    if dest.exists():
        if not dest.is_file():
            raise UnsafeWriteError(f"Destination exists and is not a regular file: {dest}")
        if not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing subtitle: {dest}")
        backup = backup_path_for(dest)
        shutil.copy2(dest, backup)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.stem}.", suffix=dest.suffix + ".part", dir=dest.parent
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return dest


def subtitle_destination(media_path: Path, language: str, extension: str = ".srt") -> Path:
    """Jellyfin-style sidecar next to the video.

    *language* must already be the ISO 639-2/B three-letter code Jellyfin
    expects, so the sidecar is named from the LOCAL video basename plus that
    code: ``Movie.mp4`` -> ``Movie.eng.srt``. Provider filenames are never
    used - the name is derived purely from the video on disk.
    """
    return media_path.with_name(f"{media_path.stem}.{language}{extension}")
