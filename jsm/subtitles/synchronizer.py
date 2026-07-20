"""ffsubsync wrapper.

The synced output is written to a temp file first; only on success is the
original subtitle backed up to ``.bak`` and atomically replaced through
:func:`jsm.subtitles.fileops.safe_write_subtitle`. The video file is only read.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from jsm.subtitles.fileops import safe_write_subtitle
from jsm.tools import resolve_tool, tool_available

SYNC_TIMEOUT_SECONDS = 15 * 60  # ffsubsync on a long movie can take a while

# Called with each progress line ffsubsync prints (may be sync or async).
ProgressCallback = Callable[[str], None] | Callable[[str], Awaitable[None]]


async def _drain(stream, on_line) -> list[str]:
    """Read a subprocess stream line by line, forwarding each to *on_line*."""
    lines: list[str] = []
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        lines.append(line)
        if on_line is not None:
            try:
                result = on_line(line)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass  # a progress callback must never break the sync
    return lines


def ffsubsync_available() -> bool:
    return tool_available("ffsubsync")


async def synchronize(
    media_path: str | Path,
    subtitle_path: str | Path,
    on_progress: ProgressCallback | None = None,
) -> tuple[bool, str]:
    """Run ffsubsync; returns (success, message). Original sub kept as .bak.

    ffsubsync's progress is streamed line by line to *on_progress* (its stderr:
    "Extracting speech segments...", "Computing alignments...", etc.) so the
    caller can show live feedback instead of a frozen 'Syncing…'.
    """
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
        ffsubsync = resolve_tool("ffsubsync") or "ffsubsync"
        proc = await asyncio.create_subprocess_exec(
            ffsubsync, str(media_path), "-i", str(subtitle_path), "-o", tmp_out,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

        async def run() -> list[str]:
            # Drain both pipes concurrently (an unread pipe can deadlock the
            # child); progress lives on stderr.
            stdout_task = asyncio.create_task(_drain(proc.stdout, None))
            stderr_lines = await _drain(proc.stderr, on_progress)
            await stdout_task
            await proc.wait()
            return stderr_lines

        try:
            stderr_lines = await asyncio.wait_for(run(), SYNC_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "ffsubsync timed out"
        if proc.returncode != 0:
            tail = stderr_lines[-1] if stderr_lines else ""
            return False, f"ffsubsync failed (exit {proc.returncode}) {tail}".rstrip()

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
