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


async def test_requires_accounts(db):
    provider, _ = make_provider(db, lambda r: httpx.Response(200), accounts=[])
    with pytest.raises(NotConfiguredError, match="accounts.conf"):
        await provider.search(["en"], query="x")


async def test_missing_api_key_is_clear_error(db):
    # The OpenSubtitles REST API requires the Api-Key header on every request
    # (a missing key means HTTP 403 for all logins) - fail fast with
    # instructions instead of letting the server reject us confusingly.
    provider, _ = make_provider(db, lambda r: httpx.Response(200))
    provider.api_key = ""
    with pytest.raises(NotConfiguredError, match="consumers"):
        await provider.search(["en"], query="x")
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="1",
                                  language="en", release_name="x")
    with pytest.raises(NotConfiguredError, match="api_key"):
        await provider.download(candidate)


async def test_builtin_default_api_key_used_when_config_empty(db, monkeypatch):
    # Like the official Jellyfin plugin: the app can ship its own key so end
    # users only ever supply username/password.
    monkeypatch.setattr("jsm.providers.opensubtitles.DEFAULT_API_KEY", "app-key")
    seen = {}

    def handler(request):
        seen.setdefault("api_key", request.headers.get("Api-Key"))
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(200, json=SEARCH_RESPONSE)

    transport = httpx.MockTransport(handler)
    provider = OpenSubtitlesProvider(
        "", AccountManager(db, [("alice", "pw")]),
        client=httpx.AsyncClient(transport=transport),
    )
    assert provider.configured is True
    assert provider.uses_default_key is True
    await provider.search(["en"], query="x")
    assert seen["api_key"] == "app-key"


async def test_config_api_key_overrides_builtin_default(db, monkeypatch):
    monkeypatch.setattr("jsm.providers.opensubtitles.DEFAULT_API_KEY", "app-key")
    seen = {}

    def handler(request):
        seen.setdefault("api_key", request.headers.get("Api-Key"))
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(200, json=SEARCH_RESPONSE)

    provider, _ = make_provider(db, handler)  # api_key="test-key" from config
    assert provider.uses_default_key is False
    await provider.search(["en"], query="x")
    assert seen["api_key"] == "test-key"


async def test_jwt_pasted_as_api_key_is_clear_error(db):
    provider, _ = make_provider(db, lambda r: httpx.Response(200))
    provider.api_key = "ey" + "x" * 150  # a JWT, not an API key
    with pytest.raises(NotConfiguredError, match="JWT"):
        await provider.search(["en"], query="x")


async def test_search_sends_api_key_and_token(db):
    seen = {}

    def handler(request):
        if request.url.path.endswith("/login"):
            seen["login_api_key"] = request.headers.get("Api-Key")
            return httpx.Response(200, json={"token": "session-token"})
        seen["params"] = dict(request.url.params)
        seen["api_key"] = request.headers.get("Api-Key")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=SEARCH_RESPONSE)

    provider, _ = make_provider(db, handler)
    results = await provider.search(["en"], moviehash="abc123", query="Inception", year=2010)
    assert seen["login_api_key"] == "test-key"  # key sent on /login too
    assert seen["api_key"] == "test-key"
    assert seen["auth"] == "Bearer session-token"
    assert seen["params"]["moviehash"] == "abc123"
    assert seen["params"]["query"] == "Inception"
    assert len(results) == 1  # entry without files skipped
    assert results[0].file_id == "111"
    assert results[0].moviehash_match is True
    assert results[0].downloads == 1234


async def test_login_403_explains_api_key_rejection(db):
    def handler(request):
        return httpx.Response(403, json={"message": "You cannot consume this service"})

    provider, _ = make_provider(db, handler)
    ok, message = await provider.validate_account("alice")
    assert ok is False
    assert "API key" in message
    assert "You cannot consume this service" in message


async def test_login_401_is_bad_credentials(db):
    def handler(request):
        return httpx.Response(401, json={"message": "Unauthorized"})

    provider, _ = make_provider(db, handler)
    ok, message = await provider.validate_account("alice")
    assert ok is False
    assert message == "invalid credentials"


async def test_vip_base_url_from_login_is_used(db):
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={
                "token": "t", "base_url": "vip-api.opensubtitles.com",
            })
        if request.url.path.endswith("/download"):
            return httpx.Response(200, json={"link": "https://files.test/s.srt"})
        if request.url.path.endswith("/subtitles"):
            return httpx.Response(200, json=SEARCH_RESPONSE)
        return httpx.Response(200, content=b"data")

    provider, _ = make_provider(db, handler)
    await provider.search(["en"], query="x")
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="1",
                                  language="en", release_name="x")
    await provider.download(candidate)
    assert "vip-api.opensubtitles.com" in hosts  # post-login calls moved over


async def test_search_retries_once_on_expired_token(db):
    state = {"searches": 0, "logins": 0}

    def handler(request):
        if request.url.path.endswith("/login"):
            state["logins"] += 1
            return httpx.Response(200, json={"token": f"t{state['logins']}"})
        state["searches"] += 1
        if state["searches"] == 1:
            return httpx.Response(401)
        assert request.headers["Authorization"] == "Bearer t1"
        return httpx.Response(200, json=SEARCH_RESPONSE)

    provider, _ = make_provider(db, handler)
    provider._tokens["alice"] = "stale"
    results = await provider.search(["en"], query="x")
    assert len(results) == 1
    assert state["logins"] == 1


async def test_api_key_sent_when_configured(db):
    seen = {}

    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "t"})
        seen["api_key"] = request.headers.get("Api-Key")
        return httpx.Response(200, json=SEARCH_RESPONSE)

    provider, _ = make_provider(db, handler)  # api_key="test-key"
    await provider.search(["en"], query="Inception")
    assert seen["api_key"] == "test-key"


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


async def test_download_no_link_out_of_quota_is_clear(db):
    # OpenSubtitles answers 200 with a message and no link when the account is
    # spent - this must read as a quota problem, not a mystery failure.
    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(200, json={
            "remaining": 0,
            "message": "You have downloaded your allowed 20 subtitles for 24h.",
        })

    provider, _ = make_provider(db, handler)
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="1",
                                  language="en", release_name="x")
    # The per-account call surfaces the server's exact reason...
    with pytest.raises(QuotaExceededError, match="20 subtitles"):
        await provider._download_as("alice", candidate)
    # ...and the rotating wrapper marks it spent and reports all-exhausted.
    with pytest.raises(QuotaExceededError, match="exhausted"):
        await provider.download(candidate)


async def test_download_bad_file_id_is_clear(db):
    provider, _ = make_provider(db, lambda r: httpx.Response(200, json={"token": "t"}))
    candidate = SubtitleCandidate(provider="opensubtitles", file_id="None",
                                  language="en", release_name="x")
    with pytest.raises(Exception, match="file id"):
        await provider.download(candidate)


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
