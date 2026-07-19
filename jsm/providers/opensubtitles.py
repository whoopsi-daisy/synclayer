"""OpenSubtitles.com REST API client (api.opensubtitles.com/api/v1).

Authentication is driven by the per-account username/password logins in
``accounts.conf``: jsm logs in lazily per account, caches the JWT, and uses it
for both search and download. An OpenSubtitles application API key is optional
- if ``api_key`` is set in config.toml it is sent as the ``Api-Key`` header,
but it is no longer required to use the tool.

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

# Rate-limit / transient-error handling.
MAX_RETRIES = 3
DEFAULT_BACKOFF = 2.0  # seconds; doubled each retry
MAX_BACKOFF = 30.0


class OpenSubtitlesError(Exception):
    pass


class NotConfiguredError(OpenSubtitlesError):
    pass


class QuotaExceededError(OpenSubtitlesError):
    pass


class RateLimitedError(OpenSubtitlesError):
    """Raised when the server keeps rate-limiting us past our retries."""


class AuthError(OpenSubtitlesError):
    """Bad username/password for an account."""


async def _sleep(seconds: float) -> None:  # pragma: no cover - trivial, patched in tests
    await asyncio.sleep(seconds)


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
        """Usable when at least one account is present. The API key is optional."""
        return bool(self.accounts.usernames)

    @property
    def has_accounts(self) -> bool:
        return bool(self.accounts.usernames)

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Api-Key"] = self.api_key
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

    def _require_accounts(self) -> None:
        if not self.has_accounts:
            raise NotConfiguredError(
                "No OpenSubtitles accounts configured - add 'username;password' "
                "lines to accounts.conf (jsm uses username/password login; an "
                "API key is optional)."
            )

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """HTTP with graceful retry on rate limits and transient network errors.

        Honors ``Retry-After`` on HTTP 429, retries 5xx and connection errors
        with exponential backoff, and finally raises a clear error.
        """
        client = await self.client()
        backoff = DEFAULT_BACKOFF
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.ReadError, httpx.WriteError,
                    httpx.PoolTimeout, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_exc = exc
                if attempt >= MAX_RETRIES:
                    raise OpenSubtitlesError(
                        f"Network error talking to OpenSubtitles: {exc}"
                    ) from exc
                await _sleep(min(backoff, MAX_BACKOFF))
                backoff *= 2
                continue

            if resp.status_code == 429:
                if attempt >= MAX_RETRIES:
                    raise RateLimitedError(
                        "OpenSubtitles rate limit hit - try again shortly."
                    )
                wait = self._retry_after(resp, backoff)
                await _sleep(min(wait, MAX_BACKOFF))
                backoff *= 2
                continue

            if 500 <= resp.status_code < 600:
                if attempt >= MAX_RETRIES:
                    raise OpenSubtitlesError(
                        f"OpenSubtitles server error (HTTP {resp.status_code}) "
                        "- try again later."
                    )
                await _sleep(min(backoff, MAX_BACKOFF))
                backoff *= 2
                continue

            return resp
        # Unreachable, but keeps type-checkers happy.
        raise OpenSubtitlesError(str(last_exc) if last_exc else "request failed")

    @staticmethod
    def _retry_after(resp: httpx.Response, fallback: float) -> float:
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return fallback

    async def _login(self, username: str) -> str:
        if username in self._tokens:
            return self._tokens[username]
        password = self.accounts.password_for(username)
        if password is None:
            raise OpenSubtitlesError(f"Unknown account: {username}")
        resp = await self._request(
            "POST", f"{self.base_url}/login",
            json={"username": username, "password": password},
            headers=self._headers(),
        )
        if resp.status_code == 401:
            raise AuthError(f"Login failed for account '{username}' (bad credentials)")
        if resp.status_code == 403:
            raise OpenSubtitlesError(
                f"Login for '{username}' was rejected (HTTP 403) - if your "
                "account requires an API key, set api_key in config.toml."
            )
        if resp.status_code != 200:
            raise OpenSubtitlesError(
                f"Login failed for '{username}': HTTP {resp.status_code}"
            )
        token = resp.json().get("token")
        if not token:
            raise OpenSubtitlesError(f"Login for '{username}' returned no token")
        self._tokens[username] = token
        return token

    async def _session_token(self) -> str:
        """A login token for read-only calls (search), from any usable account."""
        self._require_accounts()
        username = self.accounts.pick_best() or self.accounts.usernames[0]
        return await self._login(username)

    async def validate_account(self, username: str) -> tuple[bool, str]:
        """Attempt a login and report whether the credentials work."""
        try:
            await self._login(username)
        except AuthError:
            return False, "invalid credentials"
        except OpenSubtitlesError as exc:
            return False, str(exc)
        return True, "ok"

    # --------------------------------------------------------------------- API

    async def search(
        self,
        languages: list[str],
        moviehash: str | None = None,
        query: str | None = None,
        year: int | None = None,
    ) -> list[SubtitleCandidate]:
        self._require_accounts()
        token = await self._session_token()
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

        resp = await self._request(
            "GET", f"{self.base_url}/subtitles",
            params=params, headers=self._headers(token),
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
        self._require_accounts()
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
        resp = await self._request(
            "POST", f"{self.base_url}/download",
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
        file_resp = await self._request("GET", link)
        if file_resp.status_code != 200:
            raise OpenSubtitlesError(f"Fetching subtitle file failed: HTTP {file_resp.status_code}")
        return file_resp.content
