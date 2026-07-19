"""OpenSubtitles.com REST API client (api.opensubtitles.com/api/v1).

The REST API needs an application API key (``api_key`` in config.toml) plus a
user login per account; jsm logs in lazily per account and caches the JWT.
Downloads count against the logged-in account's daily quota, which the
:class:`~jsm.providers.accounts.AccountManager` tracks locally for rotation.
"""

from __future__ import annotations

import asyncio

import httpx

from jsm import __version__
from jsm.providers.accounts import AccountManager
from jsm.providers.base import SubtitleCandidate, SubtitleProvider
from jsm.subtitles.language import normalize_language

BASE_URL = "https://api.opensubtitles.com/api/v1"
USER_AGENT = f"synclayer-jsm v{__version__}"


class OpenSubtitlesError(Exception):
    pass


class NotConfiguredError(OpenSubtitlesError):
    pass


class QuotaExceededError(OpenSubtitlesError):
    pass


class OpenSubtitlesProvider(SubtitleProvider):
    name = "opensubtitles"

    def __init__(
        self,
        api_key: str,
        accounts: AccountManager,
        client: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
    ):
        self.api_key = api_key
        self.accounts = accounts
        self.base_url = base_url
        self._client = client
        self._tokens: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------------- helpers

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def has_accounts(self) -> bool:
        return bool(self.accounts.usernames)

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "Api-Key": self.api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_config(self) -> None:
        if not self.configured:
            raise NotConfiguredError(
                "No OpenSubtitles API key configured. Set api_key in config.toml "
                "(free key: https://www.opensubtitles.com/en/consumers)"
            )

    async def _login(self, username: str) -> str:
        if username in self._tokens:
            return self._tokens[username]
        password = self.accounts.password_for(username)
        if password is None:
            raise OpenSubtitlesError(f"Unknown account: {username}")
        client = await self.client()
        resp = await client.post(
            f"{self.base_url}/login",
            json={"username": username, "password": password},
            headers=self._headers(),
        )
        if resp.status_code == 401:
            raise OpenSubtitlesError(f"Login failed for account '{username}' (bad credentials)")
        if resp.status_code != 200:
            raise OpenSubtitlesError(
                f"Login failed for '{username}': HTTP {resp.status_code}"
            )
        token = resp.json().get("token")
        if not token:
            raise OpenSubtitlesError(f"Login for '{username}' returned no token")
        self._tokens[username] = token
        return token

    # --------------------------------------------------------------------- API

    async def search(
        self,
        languages: list[str],
        moviehash: str | None = None,
        query: str | None = None,
        year: int | None = None,
    ) -> list[SubtitleCandidate]:
        self._require_config()
        params: dict[str, str] = {
            "languages": ",".join(
                sorted(normalize_language(l) or l for l in languages)
            ),
            "order_by": "download_count",
        }
        if moviehash:
            params["moviehash"] = moviehash
        if query:
            params["query"] = query
        if year:
            params["year"] = str(year)

        client = await self.client()
        resp = await client.get(
            f"{self.base_url}/subtitles", params=params, headers=self._headers()
        )
        if resp.status_code != 200:
            raise OpenSubtitlesError(f"Search failed: HTTP {resp.status_code}")

        candidates: list[SubtitleCandidate] = []
        for item in resp.json().get("data", []):
            attrs = item.get("attributes", {})
            files = attrs.get("files") or []
            if not files:
                continue
            candidates.append(
                SubtitleCandidate(
                    provider=self.name,
                    file_id=str(files[0].get("file_id")),
                    language=normalize_language(attrs.get("language")) or attrs.get("language") or "und",
                    release_name=attrs.get("release") or files[0].get("file_name") or "",
                    moviehash_match=bool(attrs.get("moviehash_match")),
                    downloads=int(attrs.get("download_count") or 0),
                    hearing_impaired=bool(attrs.get("hearing_impaired")),
                    forced=bool(attrs.get("foreign_parts_only")),
                )
            )
        return candidates

    async def download(self, candidate: SubtitleCandidate) -> bytes:
        """Download subtitle content, rotating to the account with the most
        remaining quota. Raises QuotaExceededError when every account is spent."""
        self._require_config()
        if not self.has_accounts:
            raise NotConfiguredError(
                "No OpenSubtitles accounts configured - add username;password "
                "lines to accounts.conf"
            )
        async with self._lock:
            while True:
                username = self.accounts.pick_best()
                if username is None:
                    raise QuotaExceededError("All accounts have exhausted their quota")
                try:
                    content = await self._download_as(username, candidate)
                except QuotaExceededError:
                    # Server-side quota disagrees with local tracking (e.g.
                    # downloads made elsewhere). Sync local state and rotate to
                    # the next account instead of retrying this one forever.
                    self.accounts.mark_exhausted(username)
                    continue
                self.accounts.record_download(username)
                return content

    async def _download_as(
        self, username: str, candidate: SubtitleCandidate, retry: bool = True
    ) -> bytes:
        token = await self._login(username)
        client = await self.client()
        resp = await client.post(
            f"{self.base_url}/download",
            json={"file_id": int(candidate.file_id)},
            headers=self._headers(token),
        )
        if resp.status_code == 401 and retry:
            # token expired - drop it and retry once with a fresh login
            self._tokens.pop(username, None)
            return await self._download_as(username, candidate, retry=False)
        if resp.status_code == 406:
            raise QuotaExceededError(f"Account '{username}' hit its download quota")
        if resp.status_code != 200:
            raise OpenSubtitlesError(f"Download request failed: HTTP {resp.status_code}")
        body = resp.json()
        remaining = body.get("remaining")
        if isinstance(remaining, int) and remaining < 0:
            raise QuotaExceededError(f"Account '{username}' hit its download quota")
        link = body.get("link")
        if not link:
            raise OpenSubtitlesError("Download response contained no link")
        file_resp = await client.get(link)
        if file_resp.status_code != 200:
            raise OpenSubtitlesError(f"Fetching subtitle file failed: HTTP {file_resp.status_code}")
        return file_resp.content
