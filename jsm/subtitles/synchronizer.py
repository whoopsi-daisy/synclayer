"""ffsubsync wrapper.

The synced output is written to a temp file first; only on success is the
original subtitle backed up to ``.bak`` and atomically replaced through
:func:`jsm.subtitles.fileops.safe_write_subtitle`. The video file is only read.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from jsm.subtitles.fileops import safe_write_subtitle

SYNC_TIMEOUT_SECONDS = 15 * 60  # ffsubsync on a long movie can take a while


def ffsubsync_available() -> bool:
    return shutil.which("ffsubsync") is not None


async def synchronize(media_path: str | Path, subtitle_path: str | Path) -> tuple[bool, str]:
    """Run ffsubsync; returns (success, message). Original sub kept as .bak."""
    media_path = Path(media_path)
    subtitle_path = Path(subtitle_path)
    if not ffsubsync_available():
        return False, "ffsubsync is not installed (pip install ffsubsync)"
    if not media_path.is_file():
        return False, f"Video file not found: {media_path}"
    if not subtitle_path.is_file():
        return False, f"Subtitle file not found: {subtitle_path}"

    fd, tmp_out = tempfile.mkstemp(suffix=subtitle_path.suffix, prefix="jsm-sync-")
    os.close(fd)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffsubsync", str(media_path), "-i", str(subtitle_path), "-o", tmp_out,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), SYNC_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "ffsubsync timed out"
        if proc.returncode != 0:
            tail = stderr.decode(errors="replace").strip().splitlines()[-1:] if stderr else []
            return False, f"ffsubsync failed (exit {proc.returncode}) {' '.join(tail)}"

        synced = Path(tmp_out).read_bytes()
        if not synced.strip():
            return False, "ffsubsync produced an empty file - original kept unchanged"
        safe_write_subtitle(subtitle_path, synced, overwrite=True)
        return True, f"Synced (original kept as {subtitle_path.name}.bak)"
    except FileNotFoundError:
        return False, "ffsubsync is not installed (pip install ffsubsync)"
    finally:
        try:
            os.unlink(tmp_out)
        except OSError:
            pass
