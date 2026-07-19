"""ffprobe wrapper - read-only media analysis.

If ffprobe is not installed everything degrades to ``None``/empty results and
the caller shows a warning; nothing else breaks.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from jsm.tools import resolve_tool, tool_available


@dataclass
class EmbeddedSubtitle:
    index: int
    language: str | None
    codec: str | None
    forced: bool = False
    hearing_impaired: bool = False


@dataclass
class ProbeResult:
    duration: float | None = None
    container: str | None = None
    embedded_subtitles: list[EmbeddedSubtitle] = field(default_factory=list)


def ffprobe_available() -> bool:
    return tool_available("ffprobe")


def probe(path: str | Path, timeout: int = 30) -> ProbeResult | None:
    """Analyse *path* with ffprobe. Returns ``None`` when ffprobe is missing/fails."""
    ffprobe = resolve_tool("ffprobe")
    if ffprobe is None:
        return None
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=timeout, check=False,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None

    result = ProbeResult()
    fmt = data.get("format", {})
    try:
        result.duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        result.duration = None
    result.container = fmt.get("format_name")

    for stream in data.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        tags = stream.get("tags", {}) or {}
        disposition = stream.get("disposition", {}) or {}
        result.embedded_subtitles.append(
            EmbeddedSubtitle(
                index=int(stream.get("index", -1)),
                language=tags.get("language"),
                codec=stream.get("codec_name"),
                forced=bool(disposition.get("forced")),
                hearing_impaired=bool(disposition.get("hearing_impaired")),
            )
        )
    return result
