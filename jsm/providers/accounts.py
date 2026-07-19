"""OpenSubtitles account rotation with per-account rolling 24h quota."""

from __future__ import annotations

import time
from dataclasses import dataclass

from jsm.database.db import Database

DAILY_LIMIT = 20
WINDOW_SECONDS = 24 * 3600


@dataclass
class AccountQuota:
    username: str
    used: int
    remaining: int
    next_reset: float | None  # unix time when the oldest download falls out of window


class AccountManager:
    def __init__(
        self,
        db: Database,
        accounts: list[tuple[str, str]],
        daily_limit: int = DAILY_LIMIT,
    ):
        self.db = db
        self._accounts = dict(accounts)
        self.daily_limit = daily_limit

    @property
    def usernames(self) -> list[str]:
        return list(self._accounts)

    def password_for(self, username: str) -> str | None:
        return self._accounts.get(username)

    def _recent_timestamps(self, username: str, now: float | None = None) -> list[float]:
        now = time.time() if now is None else now
        cutoff = now - WINDOW_SECONDS
        return [t for t in self.db.account_timestamps(username) if t > cutoff]

    def quota(self, username: str, now: float | None = None) -> AccountQuota:
        stamps = self._recent_timestamps(username, now)
        used = len(stamps)
        return AccountQuota(
            username=username,
            used=used,
            remaining=max(0, self.daily_limit - used),
            next_reset=(min(stamps) + WINDOW_SECONDS) if stamps else None,
        )

    def all_quotas(self, now: float | None = None) -> list[AccountQuota]:
        return [self.quota(u, now) for u in self._accounts]

    def pick_best(self, now: float | None = None) -> str | None:
        """Username with the most remaining quota, or None if all exhausted."""
        best: str | None = None
        best_remaining = 0
        for username in self._accounts:
            remaining = self.quota(username, now).remaining
            if remaining > best_remaining:
                best, best_remaining = username, remaining
        return best

    def record_download(self, username: str, when: float | None = None) -> None:
        self.db.record_account_download(username, when)

    def next_available_time(self, now: float | None = None) -> float | None:
        """Earliest moment any account regains quota. None if quota is
        available right now or there are no accounts at all."""
        if not self._accounts:
            return None
        now = time.time() if now is None else now
        if self.pick_best(now) is not None:
            return None
        resets = [
            q.next_reset for q in self.all_quotas(now) if q.next_reset is not None
        ]
        return min(resets) if resets else None
