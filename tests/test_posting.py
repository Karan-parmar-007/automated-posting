"""Validation tests + posting tests with all HTTP traffic mocked (respx)."""

import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from app.platforms.bluesky import _session_cache, build_facets
from app.platforms.devto import build_article, to_devto_tags
from app.platforms.github_gist import build_gist
from app.platforms.telegram import build_text_and_entities
from app.services.content_service import PostContent, fallback_title

MASTODON = "https://mastodon.test"
OLLAMA = "http://ollama.test"
BSKY = "https://bsky.test"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
VALID_MESSAGE = "Check out our latest business platform and grow your online visibility. https://example.com"


def make_settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        llm_enabled=False,
        ollama_base_url=OLLAMA,
        verify_public_access=True,
        mastodon_base_url=MASTODON,
        mastodon_access_token="test-token",
        llm_providers="ollama",
        gemini_api_key="g-key",
        gemini_model="gemini-test",
        bluesky_service_url=BSKY,
        bluesky_handle=None,
        bluesky_app_password=None,
        telegram_bot_token=None,
        telegram_channel=None,
        github_token=None,
        devto_api_key=None,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _clear_bluesky_session_cache():
    _session_cache.clear()


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = lambda: make_settings()
    with TestClient(app) as test_client:  # context manager runs the lifespan (shared HTTP client)
        yield test_client
    app.dependency_overrides.clear()


def post_message(message: str = VALID_MESSAGE):
    with TestClient(app) as test_client:
        return test_client.post("/posting", json={"message": message})


# ---------- validation ----------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("x" * 190 + " https://a.com", "must not exceed 200"),
        ("No link here at all", "exactly one CTA link (found 0)"),
        ("Two links https://a.com and https://b.com", "exactly one CTA link (found 2)"),
        ("Bad scheme www.example.com", "must start with http"),
        ("Local host http://localhost:8000", "public domain name"),
        # Found during live testing: HttpUrl (WHATWG) accepts these hosts, DNS does not.
        ("Visit https://exa_mple!.com now", "public domain name"),
        ("Visit https://-bad-.com now", "public domain name"),
        ("Visit https://example.c0m now", "public domain name"),
        ("Visit http://192.168.1.10/admin now", "public domain name"),
        ("Visit https://exa mple.com now", "public domain name"),
        ("   ", "must not be empty"),
    ],
)
def test_invalid_messages(client, message, expected):
    resp = client.post("/posting", json={"message": message})
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert expected in body["errors"][0]["message"]


@pytest.mark.parametrize(
    "link",
    [
        "https://example.com",
        "https://sub.example.co.in/path?q=1#x",
        "http://my-site.io:8080/a",
        "https://bücher.de",
        "https://EXAMPLE.COM/",
    ],
)
def test_valid_links_accepted(link):
    from app.schemas import PostingRequest

    assert PostingRequest(message=f"Visit {link}").message == f"Visit {link}"


def test_missing_message_field(client):
    resp = client.post("/posting", json={})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "message"


def test_trailing_punctuation_not_part_of_link(client):
    with respx.mock:
        respx.post(f"{MASTODON}/api/v1/statuses").mock(
            return_value=httpx.Response(200, json={"id": "1", "url": f"{MASTODON}/@u/1", "visibility": "public"})
        )
        respx.get(f"{MASTODON}/@u/1").mock(return_value=httpx.Response(200))
        resp = client.post("/posting", json={"message": "Visit https://example.com."})
    assert resp.status_code == 200


# ---------- posting ----------


@respx.mock
def test_mastodon_success(client):
    route = respx.post(f"{MASTODON}/api/v1/statuses").mock(
        return_value=httpx.Response(200, json={"id": "123", "url": f"{MASTODON}/@user/123", "visibility": "public"})
    )
    respx.get(f"{MASTODON}/@user/123").mock(return_value=httpx.Response(200))

    resp = client.post("/posting", json={"message": VALID_MESSAGE})

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    result = next(r for r in body["results"] if r["platform"] == "Mastodon")
    assert result["status"] == "success"
    assert result["posted_url"] == f"{MASTODON}/@user/123"
    assert result["public_access"] is True
    assert result["date"] and result["time"]
    assert result["error"] is None

    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer test-token"
    assert "Idempotency-Key" in sent.headers
    assert b"visibility=public" in sent.content


@respx.mock
def test_mastodon_auth_failure(client):
    respx.post(f"{MASTODON}/api/v1/statuses").mock(
        return_value=httpx.Response(401, json={"error": "The access token is invalid"})
    )
    body = client.post("/posting", json={"message": VALID_MESSAGE}).json()
    result = next(r for r in body["results"] if r["platform"] == "Mastodon")
    assert result["status"] == "failed"
    assert result["posted_url"] is None
    assert result["date"] is None
    assert "The access token is invalid" in result["error"]


@respx.mock
def test_mastodon_timeout(client):
    respx.post(f"{MASTODON}/api/v1/statuses").mock(side_effect=httpx.ReadTimeout("slow"))
    body = client.post("/posting", json={"message": VALID_MESSAGE}).json()
    result = next(r for r in body["results"] if r["platform"] == "Mastodon")
    assert result["status"] == "failed"
    assert result["error"] == "Request timed out."


def test_unconfigured_platform_reports_failure():
    app.dependency_overrides[get_settings] = lambda: make_settings(mastodon_access_token=None)
    try:
        body = post_message().json()
    finally:
        app.dependency_overrides.clear()
    result = next(r for r in body["results"] if r["platform"] == "Mastodon")
    assert result["status"] == "failed"
    assert "not configured" in result["error"]


# ---------- Ollama enrichment ----------


def _mock_mastodon():
    route = respx.post(f"{MASTODON}/api/v1/statuses").mock(
        return_value=httpx.Response(200, json={"id": "9", "url": f"{MASTODON}/@u/9", "visibility": "public"})
    )
    respx.get(f"{MASTODON}/@u/9").mock(return_value=httpx.Response(200))
    return route


@respx.mock
def test_ollama_hashtags_added_to_mastodon_post():
    app.dependency_overrides[get_settings] = lambda: make_settings(llm_enabled=True)
    ollama = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"title": "Grow Your Visibility", "hashtags": ["#Business", "Growth!", "business", "SEO"]}
                    ),
                }
            },
        )
    )
    mastodon = _mock_mastodon()
    try:
        resp = post_message()
    finally:
        app.dependency_overrides.clear()

    assert resp.json()["results"][0]["status"] == "success"
    sent_llm = json.loads(ollama.calls.last.request.content)
    assert sent_llm["think"] is False and sent_llm["stream"] is False and "properties" in sent_llm["format"]
    status = parse_qs(mastodon.calls.last.request.content.decode())["status"][0]
    assert status == f"{VALID_MESSAGE}\n\n#Business #Growth #SEO"  # cleaned + de-duplicated


@respx.mock
def test_ollama_down_falls_back_to_plain_message():
    app.dependency_overrides[get_settings] = lambda: make_settings(llm_enabled=True)
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    mastodon = _mock_mastodon()
    try:
        resp = post_message()
    finally:
        app.dependency_overrides.clear()

    assert resp.json()["success"] is True
    status = parse_qs(mastodon.calls.last.request.content.decode())["status"][0]
    assert status == VALID_MESSAGE


@respx.mock
def test_ollama_invalid_json_falls_back():
    app.dependency_overrides[get_settings] = lambda: make_settings(llm_enabled=True)
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "sorry, I cannot"}})
    )
    mastodon = _mock_mastodon()
    try:
        post_message()
    finally:
        app.dependency_overrides.clear()
    assert parse_qs(mastodon.calls.last.request.content.decode())["status"][0] == VALID_MESSAGE


def test_hashtags_trimmed_to_fit_limit():
    content = PostContent(message="x" * 490, cta_link="https://a.com", title="t", hashtags=["One", "Two", "Three"])
    assert content.message_with_hashtags(1000) == "x" * 490 + "\n\n#One #Two #Three"
    assert content.message_with_hashtags(500) == "x" * 490 + "\n\n#One"  # "#Two" would exceed 500
    assert content.message_with_hashtags(495) == "x" * 490


def test_fallback_title_strips_link():
    assert fallback_title(VALID_MESSAGE, "https://example.com") == (
        "Check out our latest business platform and grow your online visibility."
    )


# ---------- Gemini (first provider) ----------


def _gemini_reply(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


@respx.mock
def test_gemini_used_first_then_ollama_not_called():
    app.dependency_overrides[get_settings] = lambda: make_settings(llm_enabled=True, llm_providers="gemini,ollama")
    gemini = respx.post(GEMINI_URL).mock(return_value=_gemini_reply({"title": "T", "hashtags": ["Gem"]}))
    ollama = respx.post(f"{OLLAMA}/api/chat")
    mastodon = _mock_mastodon()
    try:
        post_message()
    finally:
        app.dependency_overrides.clear()

    req = gemini.calls.last.request
    assert req.headers["x-goog-api-key"] == "g-key"
    cfg = json.loads(req.content)["generationConfig"]
    assert cfg["responseMimeType"] == "application/json" and "properties" in cfg["responseJsonSchema"]
    assert not ollama.called
    assert parse_qs(mastodon.calls.last.request.content.decode())["status"][0].endswith("#Gem")


@respx.mock
def test_gemini_overloaded_falls_back_to_ollama():
    app.dependency_overrides[get_settings] = lambda: make_settings(llm_enabled=True, llm_providers="gemini,ollama")
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "high demand"}}))
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200, json={"message": {"content": json.dumps({"title": "T", "hashtags": ["Local"]})}}
        )
    )
    mastodon = _mock_mastodon()
    try:
        post_message()
    finally:
        app.dependency_overrides.clear()
    assert parse_qs(mastodon.calls.last.request.content.decode())["status"][0].endswith("#Local")


# ---------- Bluesky ----------

DID = "did:plc:abc123"


def _bsky_settings(**kw):
    return make_settings(
        mastodon_access_token=None, bluesky_handle="me.bsky.social", bluesky_app_password="app-pass", **kw
    )


def _mock_bsky_login():
    return respx.post(f"{BSKY}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(
            200, json={"accessJwt": "jwt", "refreshJwt": "r", "did": DID, "handle": "me.bsky.social"}
        )
    )


@respx.mock
def test_bluesky_success():
    app.dependency_overrides[get_settings] = lambda: _bsky_settings()
    login = _mock_bsky_login()
    create = respx.post(f"{BSKY}/xrpc/com.atproto.repo.createRecord").mock(
        return_value=httpx.Response(200, json={"uri": f"at://{DID}/app.bsky.feed.post/3kxyz", "cid": "bafy"})
    )
    thread = respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(
        return_value=httpx.Response(200, json={"thread": {}})
    )
    try:
        body = post_message().json()
    finally:
        app.dependency_overrides.clear()

    result = next(r for r in body["results"] if r["platform"] == "Bluesky")
    assert result["status"] == "success"
    assert result["posted_url"] == "https://bsky.app/profile/me.bsky.social/post/3kxyz"
    assert result["public_access"] is True
    assert json.loads(login.calls.last.request.content) == {"identifier": "me.bsky.social", "password": "app-pass"}

    sent = json.loads(create.calls.last.request.content)
    assert create.calls.last.request.headers["Authorization"] == "Bearer jwt"
    assert sent["repo"] == DID and sent["collection"] == "app.bsky.feed.post"
    record = sent["record"]
    assert record["$type"] == "app.bsky.feed.post" and record["text"] == VALID_MESSAGE
    assert record["createdAt"].endswith("Z")
    assert record["facets"][0]["features"][0] == {"$type": "app.bsky.richtext.facet#link", "uri": "https://example.com"}
    assert record["embed"]["external"]["uri"] == "https://example.com"
    assert thread.calls.last.request.url.params["uri"] == f"at://{DID}/app.bsky.feed.post/3kxyz"


@respx.mock
def test_bluesky_bad_password():
    app.dependency_overrides[get_settings] = lambda: _bsky_settings()
    respx.post(f"{BSKY}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(
            401, json={"error": "AuthenticationRequired", "message": "Invalid identifier or password"}
        )
    )
    try:
        body = post_message().json()
    finally:
        app.dependency_overrides.clear()
    result = next(r for r in body["results"] if r["platform"] == "Bluesky")
    assert result["status"] == "failed"
    assert result["error"] == "Authentication failed: AuthenticationRequired: Invalid identifier or password"


@respx.mock
def test_bluesky_failure_does_not_stop_mastodon():
    app.dependency_overrides[get_settings] = lambda: make_settings(
        bluesky_handle="me.bsky.social", bluesky_app_password="app-pass"
    )
    respx.post(f"{BSKY}/xrpc/com.atproto.server.createSession").mock(side_effect=httpx.ConnectError("down"))
    _mock_mastodon()
    try:
        body = post_message().json()
    finally:
        app.dependency_overrides.clear()
    by_name = {r["platform"]: r for r in body["results"]}
    assert body["success"] is True
    assert by_name["Mastodon"]["status"] == "success"
    assert by_name["Bluesky"]["status"] == "failed" and "Network error" in by_name["Bluesky"]["error"]


def test_bluesky_facets_use_utf8_byte_offsets():
    text = "Café 🚀 deals https://a.com\n\n#Sale"
    facets = build_facets(text, "https://a.com", ["Sale"])
    raw = text.encode("utf-8")
    assert [raw[f["index"]["byteStart"] : f["index"]["byteEnd"]].decode() for f in facets] == ["https://a.com", "#Sale"]
    assert facets[1]["features"][0] == {"$type": "app.bsky.richtext.facet#tag", "tag": "Sale"}


# ---------- Telegram ----------

TG_SEND = "https://api.telegram.org/bot123:ABC/sendMessage"


def _tg_settings(**kw):
    return make_settings(mastodon_access_token=None, telegram_bot_token="123:ABC", telegram_channel="@my_channel", **kw)


def _post_and_get(platform: str) -> dict:
    try:
        body = post_message().json()
    finally:
        app.dependency_overrides.clear()
    return next(r for r in body["results"] if r["platform"] == platform)


@respx.mock
def test_telegram_success_public_channel():
    app.dependency_overrides[get_settings] = lambda: _tg_settings()
    send = respx.post(TG_SEND).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 42,
                    "date": 1,
                    "chat": {"id": -1001234, "type": "channel", "username": "my_channel"},
                },
            },
        )
    )

    result = _post_and_get("Telegram")

    assert result["status"] == "success"
    assert result["posted_url"] == "https://t.me/my_channel/42"
    assert result["public_access"] is True
    sent = json.loads(send.calls.last.request.content)
    assert sent["chat_id"] == "@my_channel" and sent["text"] == VALID_MESSAGE
    assert sent["link_preview_options"]["url"] == "https://example.com"
    assert {"type": "url", "offset": 72, "length": 19} in sent["entities"]


@respx.mock
def test_telegram_bot_not_admin():
    app.dependency_overrides[get_settings] = lambda: _tg_settings()
    respx.post(TG_SEND).mock(
        return_value=httpx.Response(
            403,
            json={"ok": False, "error_code": 403, "description": "Forbidden: bot is not a member of the channel chat"},
        )
    )
    result = _post_and_get("Telegram")
    assert result["status"] == "failed"
    assert result["error"] == "Error 403: Forbidden: bot is not a member of the channel chat"


@respx.mock
def test_telegram_flood_limit():
    app.dependency_overrides[get_settings] = lambda: _tg_settings()
    respx.post(TG_SEND).mock(
        return_value=httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            },
        )
    )
    assert _post_and_get("Telegram")["error"] == "Rate limit exceeded: retry after 17s."


@respx.mock
def test_telegram_private_channel_not_public():
    app.dependency_overrides[get_settings] = lambda: _tg_settings()
    respx.post(TG_SEND).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 7, "date": 1, "chat": {"id": -1009876, "type": "channel"}}}
        )
    )
    result = _post_and_get("Telegram")
    assert result["posted_url"] == "https://t.me/c/9876/7"
    assert result["public_access"] is False


def test_telegram_entities_use_utf16_offsets():
    content = PostContent(
        message="🚀 Launch day https://a.com",
        cta_link="https://a.com",
        title="Big 🚀 News",
        hashtags=["Launch"],
        generated_by="gemini",
    )
    text, entities = build_text_and_entities(content, include_title=True)
    assert text == "Big 🚀 News\n\n🚀 Launch day https://a.com\n\n#Launch"
    utf16 = text.encode("utf-16-le")
    pieces = [utf16[e["offset"] * 2 : (e["offset"] + e["length"]) * 2].decode("utf-16-le") for e in entities]
    assert pieces == ["Big 🚀 News", "https://a.com", "#Launch"]
    assert [e["type"] for e in entities] == ["bold", "url", "hashtag"]


def test_telegram_no_title_when_llm_fallback():
    content = PostContent(message="Hi https://a.com", cta_link="https://a.com", title="Hi")
    text, entities = build_text_and_entities(content, include_title=True)
    assert text == "Hi https://a.com" and [e["type"] for e in entities] == ["url"]


# ---------- GitHub Gist ----------

GISTS = "https://api.github.com/gists"


def _gist_settings(**kw):
    return make_settings(mastodon_access_token=None, github_token="ghp_test", **kw)


@respx.mock
def test_gist_success():
    app.dependency_overrides[get_settings] = lambda: _gist_settings()
    create = respx.post(GISTS).mock(
        return_value=httpx.Response(
            201, json={"id": "abc", "html_url": "https://gist.github.com/me/abc", "public": True}
        )
    )
    respx.get("https://gist.github.com/me/abc").mock(return_value=httpx.Response(200))

    result = _post_and_get("GitHub Gist")

    assert result["status"] == "success"
    assert result["posted_url"] == "https://gist.github.com/me/abc"
    assert result["public_access"] is True
    req = create.calls.last.request
    assert req.headers["Authorization"] == "Bearer ghp_test"
    assert req.headers["X-GitHub-Api-Version"] == "2026-03-10"
    sent = json.loads(req.content)
    assert sent["public"] is True
    ((filename, file),) = sent["files"].items()
    assert filename.endswith(".md") and VALID_MESSAGE in file["content"]


@respx.mock
def test_gist_missing_permission():
    app.dependency_overrides[get_settings] = lambda: _gist_settings()
    respx.post(GISTS).mock(
        return_value=httpx.Response(
            403,
            json={"message": "Resource not accessible by personal access token"},
            headers={"x-ratelimit-remaining": "4999"},
        )
    )
    result = _post_and_get("GitHub Gist")
    assert result["status"] == "failed"
    assert "Resource not accessible" in result["error"] and "Gists" in result["error"]


@respx.mock
def test_gist_bad_token():
    app.dependency_overrides[get_settings] = lambda: _gist_settings()
    respx.post(GISTS).mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))
    assert _post_and_get("GitHub Gist")["error"].startswith("Authentication failed: Bad credentials")


def test_gist_markdown_layout():
    content = PostContent(
        message=VALID_MESSAGE,
        cta_link="https://example.com",
        title="Grow Your Visibility!",
        hashtags=["Business"],
        generated_by="gemini",
    )
    filename, description, body = build_gist(content)
    assert filename == "grow-your-visibility.md"
    assert description == "Grow Your Visibility!"
    assert body.startswith("# Grow Your Visibility!\n\n" + VALID_MESSAGE)
    assert "[https://example.com](https://example.com)" in body and "`#Business`" in body


# ---------- DEV Community ----------

DEVTO = "https://dev.to/api/articles"


def _devto_settings(**kw):
    return make_settings(mastodon_access_token=None, devto_api_key="dev-key", **kw)


@respx.mock
def test_devto_success():
    app.dependency_overrides[get_settings] = lambda: _devto_settings()
    create = respx.post(DEVTO).mock(
        return_value=httpx.Response(201, json={"id": 1, "url": "https://dev.to/me/grow-1abc", "published": True})
    )
    respx.get("https://dev.to/me/grow-1abc").mock(return_value=httpx.Response(200))

    result = _post_and_get("DEV Community")

    assert result["status"] == "success"
    assert result["posted_url"] == "https://dev.to/me/grow-1abc"
    assert result["public_access"] is True
    req = create.calls.last.request
    assert req.headers["api-key"] == "dev-key"
    assert req.headers["Accept"] == "application/vnd.forem.api-v1+json"
    article = json.loads(req.content)["article"]
    assert article["published"] is True and VALID_MESSAGE in article["body_markdown"]


@respx.mock
def test_devto_validation_error():
    app.dependency_overrides[get_settings] = lambda: _devto_settings()
    respx.post(DEVTO).mock(
        return_value=httpx.Response(
            422, json={"error": "Title has already been used in the last five minutes", "status": 422}
        )
    )
    result = _post_and_get("DEV Community")
    assert result["status"] == "failed"
    assert result["error"] == "HTTP 422: Title has already been used in the last five minutes"


@respx.mock
def test_devto_rate_limited():
    app.dependency_overrides[get_settings] = lambda: _devto_settings()
    respx.post(DEVTO).mock(return_value=httpx.Response(429, headers={"retry-after": "30"}, json={}))
    assert _post_and_get("DEV Community")["error"] == "Rate limit exceeded: retry after 30s."


def test_devto_tags_and_article_shape():
    assert to_devto_tags(["BusinessGrowth", "SEO", "seo", "Web-Dev", "AI", "Extra"]) == "businessgrowth,seo,webdev,ai"
    content = PostContent(message=VALID_MESSAGE, cta_link="https://example.com", title="Grow", hashtags=["SEO"])
    article = build_article(content)["article"]
    assert article["title"] == "Grow" and article["tags"] == "seo"
    assert article["description"] == "Check out our latest business platform and grow your online visibility."


# ---------- cross-cutting ----------


@respx.mock
def test_secrets_are_scrubbed_from_errors():
    app.dependency_overrides[get_settings] = lambda: make_settings(
        mastodon_access_token=None, telegram_bot_token="123:SECRET", telegram_channel="@c"
    )
    respx.post("https://api.telegram.org/bot123:SECRET/sendMessage").mock(
        side_effect=httpx.ConnectError("failed to connect to /bot123:SECRET/sendMessage")
    )
    error = _post_and_get("Telegram")["error"]
    assert "123:SECRET" not in error and "***" in error


@respx.mock
def test_all_five_platforms_post_concurrently():
    """Every platform configured and mocked: all succeed, one result each, in registry order."""
    app.dependency_overrides[get_settings] = lambda: make_settings(
        bluesky_handle="me.bsky.social",
        bluesky_app_password="p",
        telegram_bot_token="123:ABC",
        telegram_channel="@my_channel",
        github_token="ghp",
        devto_api_key="dev",
    )
    _mock_mastodon()
    _mock_bsky_login()
    respx.post(f"{BSKY}/xrpc/com.atproto.repo.createRecord").mock(
        return_value=httpx.Response(200, json={"uri": f"at://{DID}/app.bsky.feed.post/r1", "cid": "c"})
    )
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread").mock(return_value=httpx.Response(200))
    respx.post(TG_SEND).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 1, "chat": {"id": -1, "type": "channel", "username": "my_channel"}},
            },
        )
    )
    respx.post(GISTS).mock(
        return_value=httpx.Response(201, json={"html_url": "https://gist.github.com/g", "public": True})
    )
    respx.get("https://gist.github.com/g").mock(return_value=httpx.Response(200))
    respx.post(DEVTO).mock(return_value=httpx.Response(201, json={"url": "https://dev.to/a", "published": True}))
    respx.get("https://dev.to/a").mock(return_value=httpx.Response(200))
    try:
        body = post_message().json()
    finally:
        app.dependency_overrides.clear()

    assert body["success"] is True
    assert [r["platform"] for r in body["results"]] == [
        "Mastodon",
        "Bluesky",
        "Telegram",
        "GitHub Gist",
        "DEV Community",
    ]
    assert all(r["status"] == "success" and r["public_access"] for r in body["results"])
