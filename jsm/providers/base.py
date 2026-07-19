"""Provider interface: anything that can search for and download subtitles."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SubtitleCandidate:
    provider: str
    file_id: str
    language: str
    release_name: str
    moviehash_match: bool = False
    downloads: int = 0
    hearing_impaired: bool = False
    forced: bool = False
    extension: str = ".srt"
    # filled in by the matcher
    confidence: float = 0.0
    match_reason: str = ""


class SubtitleProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def search(
        self,
        languages: list[str],
        moviehash: str | None = None,
        query: str | None = None,
        year: int | None = None,
    ) -> list[SubtitleCandidate]:
        """Search for subtitles; hash and text criteria may be combined."""

    @abstractmethod
    async def download(self, candidate: SubtitleCandidate) -> bytes:
        """Download the subtitle content for a candidate."""

    async def close(self) -> None:  # pragma: no cover - trivial default
        pass
