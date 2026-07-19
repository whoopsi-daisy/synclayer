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
from jsm.tools import resolve_tool, tool_available

CLEAN_TIMEOUT_SECONDS = 120


def subscleaner_available() -> bool:
    return tool_available("subscleaner")


async def _run_subscleaner(subscleaner: str, target: str) -> tuple[int | None, bytes]:
    """Feed *target* to subscleaner on stdin (its only input channel).

    Newer subscleaner (the rogs/2.x line) takes filenames on stdin and
    supports --force and an isolated --db-location; we clean a throwaway copy,
    so we always want it processed and never want to touch the user's real
    tracking database. Older builds that predate those flags exit with an
    argparse usage error (code 2) - fall back to a bare stdin invocation.
    """
    async def invoke(args: list[str]) -> tuple[int | None, bytes]:
        proc = await asyncio.create_subprocess_exec(
            subscleaner, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=(target + "\n").encode()),
                CLEAN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode, stderr or b""

    fd, tmp_db = tempfile.mkstemp(suffix=".db", prefix="jsm-clean-db-")
    os.close(fd)
    os.unlink(tmp_db)  # let subscleaner create it fresh
    try:
        code, stderr = await invoke(["--force", "--db-location", tmp_db])
        if code == 2:  # usage error -> older subscleaner without those flags
            code, stderr = await invoke([])
        return code, stderr
    finally:
        for leftover in (tmp_db, tmp_db + "-wal", tmp_db + "-shm"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


async def clean(subtitle_path: str | Path) -> tuple[bool, str]:
    """Run subscleaner on *subtitle_path*. Returns (changed, message).

    ``changed`` is True only when the file was actually rewritten. A missing
    tool or a no-op cleanup returns (False, reason) and never raises.
    """
    subtitle_path = Path(subtitle_path)
    if not subscleaner_available():
        return False, ("subscleaner not found - install it (pip install "
                       "subscleaner) or set subscleaner_path in config.toml")
    if not subtitle_path.is_file():
        return False, f"Subtitle file not found: {subtitle_path}"

    fd, tmp = tempfile.mkstemp(suffix=subtitle_path.suffix, prefix="jsm-clean-")
    os.close(fd)
    try:
        shutil.copy2(subtitle_path, tmp)
        original = Path(tmp).read_bytes()
        try:
            subscleaner = resolve_tool("subscleaner") or "subscleaner"
            code, stderr = await _run_subscleaner(subscleaner, tmp)
        except asyncio.TimeoutError:
            return False, "subscleaner timed out - original kept unchanged"
        except FileNotFoundError:
            return False, ("subscleaner not found - install it (pip install "
                           "subscleaner) or set subscleaner_path in config.toml")
        if code not in (0, None):
            tail = stderr.decode(errors="replace").strip().splitlines()[-1:] if stderr else []
            return False, f"subscleaner failed (exit {code}) {' '.join(tail)}"

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
