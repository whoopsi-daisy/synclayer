"""OpenSubtitles moviehash.

64-bit checksum of file size + the first and last 64 KiB, read-only.
Reference: https://trac.opensubtitles.org/projects/opensubtitles/wiki/HashSourceCodes
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

CHUNK_SIZE = 65536  # 64 KiB


def compute_moviehash(path: str | Path) -> str | None:
    """Return the 16-hex-digit hash, or ``None`` for files under 128 KiB."""
    filesize = os.path.getsize(path)
    if filesize < CHUNK_SIZE * 2:
        return None

    fmt = "<%dQ" % (CHUNK_SIZE // 8)
    hash_ = filesize
    with open(path, "rb") as fh:
        for word in struct.unpack(fmt, fh.read(CHUNK_SIZE)):
            hash_ = (hash_ + word) & 0xFFFFFFFFFFFFFFFF
        fh.seek(filesize - CHUNK_SIZE)
        for word in struct.unpack(fmt, fh.read(CHUNK_SIZE)):
            hash_ = (hash_ + word) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % hash_
