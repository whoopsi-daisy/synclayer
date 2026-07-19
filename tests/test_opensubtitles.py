import httpx
import pytest

from jsm.providers.accounts import AccountManager
from jsm.providers.base import SubtitleCandidate
from jsm.providers.opensubtitles import (
    NotConfiguredError,
    OpenSubtitlesProvider,
    QuotaExceededError,
)

SEARCH_RESPONSE = {
    "data": [
        {
            "attributes": {
                "language": "en",
                "release": "Inception.2010.1080p.YIFY",
                "moviehash_match": True,
                "download_count": 1234,
                "hearing_impaired": False,
                "files": [{"file_id": 111, "file_name": "inception.srt"}],
            }
        },
        {
            "attributes": {
                "language": "en",
                "release": "no files - skipped",
                "files": [],
            }
        },
    ]
}


def make_provider(db, handler, accounts=None):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    mgr = AccountManager(db, accounts if accounts is not None else [("alice", "pw")])
    return OpenSubtitlesProvider("test-key", mgr, client=client), mgr


async def test_requires_api_key(db):
    provider, _ = make_provider(db, lambda r: httpx.Response(200))
    provider.api_key = ""
    with pytest.raises(NotConfiguredError):
        await provider.search(["en"], query="x")


async def test_search_parses_candidates(db):
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        seen["api_key"] = request.headers.get("Api-Key")
        return httpx.Response(200, json=SEARCH_RESPONSE)

    provider, _ = make_provider(db, handler)
    results = await provider.search(["en"], moviehash="abc123", query="Inception", year=2010)
    assert seen["api_key"] == "test-key"
    assert seen["params"]["moviehash"] == "abc123"
    assert seen["params"]["query"] == "Inception"
    assert seen["params"]["year"] == "2010"
    assert len(results) == 1  # entry without files skipped
    assert results[0].file_id == "111"
    assert results[0].moviehash_match is True
    assert results[0].downloads == 1234


async def test_download_logs_in_and_records_quota(db):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "jwt-token"})
        if request.url.path.endswith("/download"):
            assert request.headers["Authorization"] == "Bearer jwt-token"
            return httpx.Response(200, json={"link": "https://files.test/sub.srt",
                                             "remaining": 19})
        if request.url.path.endswith("/sub.srt"):
            return httpx.Response(200, content=b"subtitle content")
        return httpx.Response(404)

    provider, mgr = make_provider(db, handler)
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="111",
                                  language="en", release_name="x")
    content = await provider.download(candidate)
    assert content == b"subtitle content"
    assert mgr.quota("alice").used == 1
    assert any(c == "POST" and p.endswith("/login") for c, p in calls)


async def test_download_rotates_to_freshest_account(db):
    logins = []

    def handler(request):
        if request.url.path.endswith("/login"):
            import json

            logins.append(json.loads(request.content)["username"])
            return httpx.Response(200, json={"token": "t"})
        if request.url.path.endswith("/download"):
            return httpx.Response(200, json={"link": "https://files.test/s.srt"})
        return httpx.Response(200, content=b"data")

    provider, mgr = make_provider(db, handler, accounts=[("a", "pa"), ("b", "pb")])
    for _ in range(3):
        mgr.record_download("a")
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="1",
                                  language="en", release_name="x")
    await provider.download(candidate)
    assert logins == ["b"]


async def test_quota_error_on_406(db):
    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(406)

    provider, _ = make_provider(db, handler)
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="1",
                                  language="en", release_name="x")
    with pytest.raises(QuotaExceededError):
        await provider.download(candidate)


async def test_no_accounts_is_clear_error(db):
    provider, _ = make_provider(db, lambda r: httpx.Response(200), accounts=[])
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="1",
                                  language="en", release_name="x")
    with pytest.raises(NotConfiguredError, match="accounts.conf"):
        await provider.download(candidate)


async def test_expired_token_retries_once(db):
    state = {"downloads": 0}

    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": f"t{state['downloads']}"})
        if request.url.path.endswith("/download"):
            state["downloads"] += 1
            if state["downloads"] == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"link": "https://files.test/s.srt"})
        return httpx.Response(200, content=b"data")

    provider, _ = make_provider(db, handler)
    provider._tokens["alice"] = "stale"
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="1",
                                  language="en", release_name="x")
    assert await provider.download(candidate) == b"data"
