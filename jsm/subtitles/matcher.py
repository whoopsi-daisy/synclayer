"""Candidate ranking: hash matches first, then filename similarity.

Filename parsing uses guessit, which understands scene naming conventions
(title, year, release group like YIFY/SALT, resolution, codec).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from guessit import guessit

from jsm.providers.base import SubtitleCandidate

HASH_CONFIDENCE = 0.99
NAME_CONFIDENCE_CAP = 0.95


@dataclass
class MediaGuess:
    title: str | None = None
    year: int | None = None
    release_group: str | None = None
    screen_size: str | None = None
    video_codec: str | None = None


def guess_media(filename: str) -> MediaGuess:
    try:
        info = guessit(filename)
    except Exception:
        info = {}
    year = info.get("year")
    return MediaGuess(
        title=str(info.get("title")) if info.get("title") else None,
        year=int(year) if isinstance(year, int) else None,
        release_group=str(info.get("release_group")) if info.get("release_group") else None,
        screen_size=str(info.get("screen_size")) if info.get("screen_size") else None,
        video_codec=str(info.get("video_codec")) if info.get("video_codec") else None,
    )


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def score_candidate(guess: MediaGuess, candidate: SubtitleCandidate) -> tuple[float, str]:
    """Confidence in [0,1] plus a human-readable reason."""
    if candidate.moviehash_match:
        return HASH_CONFIDENCE, "hash match"

    cand_guess = guess_media(candidate.release_name)
    score = 0.0
    reasons: list[str] = []

    if guess.title and cand_guess.title:
        ratio = SequenceMatcher(
            None, _norm_title(guess.title), _norm_title(cand_guess.title)
        ).ratio()
        score += 0.55 * ratio
        reasons.append(f"title {ratio:.0%}")
    if guess.year and cand_guess.year:
        if guess.year == cand_guess.year:
            score += 0.20
            reasons.append("year")
        else:
            score -= 0.20  # wrong year is a strong negative signal
            reasons.append("year mismatch")
    if guess.release_group and cand_guess.release_group:
        if guess.release_group.lower() == cand_guess.release_group.lower():
            score += 0.15
            reasons.append(f"group {guess.release_group}")
    if guess.screen_size and cand_guess.screen_size == guess.screen_size:
        score += 0.06
        reasons.append(guess.screen_size)
    if guess.video_codec and cand_guess.video_codec == guess.video_codec:
        score += 0.04
        reasons.append(guess.video_codec)

    score = max(0.0, min(score, NAME_CONFIDENCE_CAP))
    return score, "filename: " + ", ".join(reasons) if reasons else "no signals"


def rank_candidates(
    media_filename: str,
    candidates: list[SubtitleCandidate],
    language: str | None = None,
) -> list[SubtitleCandidate]:
    """Score, filter by language, and sort best-first (confidence, downloads)."""
    guess = guess_media(media_filename)
    ranked: list[SubtitleCandidate] = []
    for candidate in candidates:
        if language and candidate.language != language:
            continue
        candidate.confidence, candidate.match_reason = score_candidate(guess, candidate)
        ranked.append(candidate)
    ranked.sort(key=lambda c: (c.confidence, c.downloads), reverse=True)
    return ranked
