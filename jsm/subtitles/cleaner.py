"""subscleaner wrapper - strips advertising/spam lines from subtitle files.

subscleaner (https://pypi.org/project/subscleaner/) edits a subtitle file in
place. To keep our safety guarantees we never let it touch the original: we
copy the subtitle to a temp file, clean the copy, and only if it still has
content do we atomically write it back through
:func:`jsm.subtitles.fileops.safe_write_subtitle` (which keeps a ``.bak``).
The video file is never involved.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from jsm.subtitles.fileops import safe_write_subtitle

CLEAN_TIMEOUT_SECONDS = 120


def subscleaner_available() -> bool:
    return shutil.which("subscleaner") is not None


async def clean(subtitle_path: str | Path) -> tuple[bool, str]:
    """Run subscleaner on *subtitle_path*. Returns (changed, message).

    ``changed`` is True only when the file was actually rewritten. A missing
    tool or a no-op cleanup returns (False, reason) and never raises.
    """
    subtitle_path = Path(subtitle_path)
    if not subscleaner_available():
        return False, "subscleaner is not installed (pip install subscleaner)"
    if not subtitle_path.is_file():
        return False, f"Subtitle file not found: {subtitle_path}"

    fd, tmp = tempfile.mkstemp(suffix=subtitle_path.suffix, prefix="jsm-clean-")
    os.close(fd)
    try:
        shutil.copy2(subtitle_path, tmp)
        original = Path(tmp).read_bytes()
        try:
            proc = await asyncio.create_subprocess_exec(
                "subscleaner", tmp,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), CLEAN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "subscleaner timed out - original kept unchanged"
        except FileNotFoundError:
            return False, "subscleaner is not installed (pip install subscleaner)"
        if proc.returncode not in (0, None):
            tail = stderr.decode(errors="replace").strip().splitlines()[-1:] if stderr else []
            return False, f"subscleaner failed (exit {proc.returncode}) {' '.join(tail)}"

        cleaned = Path(tmp).read_bytes()
        if not cleaned.strip():
            return False, "subscleaner produced an empty file - original kept unchanged"
        if cleaned == original:
            return False, "nothing to clean"
        safe_write_subtitle(subtitle_path, cleaned, overwrite=True)
        return True, f"cleaned ads/spam (original kept as {subtitle_path.name}.bak)"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
