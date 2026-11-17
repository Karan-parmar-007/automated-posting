"""Bluesky (AT Protocol) integration.

API docs:
  - Creating posts:  https://docs.bsky.app/docs/advanced-guides/posts
  - createSession:   https://docs.bsky.app/docs/api/com-atproto-server-create-session
  - createRecord:    https://docs.bsky.app/docs/api/com-atproto-repo-create-record
  - Lexicons:        app.bsky.feed.post, app.bsky.richtext.facet, app.bsky.embed.external
                     (github.com/bluesky-social/atproto/tree/main/lexicons)
  - Rate limits:     https://docs.bsky.app/docs/advanced-guides/rate-limits

Auth: handle + app password (Settings → Privacy and security → App passwords).

Notes:
  - Post text is limited to 300 graphemes.
  - Links and hashtags are NOT auto-detected: they must be sent as "facets" whose
    positions are UTF-8 *byte* offsets into the text.
"""

import time
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from app.platforms.base import BasePlatform, PlatformError, PublishedPost
from app.services.content_service import PostContent

MAX_GRAPHEMES = 300
PUBLIC_APPVIEW = "https://public.api.bsky.app"
SESSION_TTL_SECONDS = 60 * 60  # access JWTs are short-lived; createSession is rate limited

# Cached per handle so we don't call createSession (rate limited) on every request.
_session_cache: dict[str, tuple[float, dict]] = {}


def _byte_slice(text: str, start_char: int, end_char: int) -> dict:
    return {
        "byteStart": len(text[:start_char].encode("utf-8")),
        "byteEnd": len(text[:end_char].encode("utf-8")),
    }


def build_facets(text: str, link: str, hashtags: list[str]) -> list[dict]:
    """Rich-text facets making the CTA link and hashtags clickable."""
    facets: list[dict] = []
    start = text.find(link)
    if start != -1:
        facets.append(
            {
                "index": _byte_slice(text, start, start + len(link)),
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": link}],
            }
        )
    # Hashtags are appended after the message, so search from the end of the link onward.
    cursor = start + len(link) if start != -1 else 0
    for tag in hashtags:
        token = f"#{tag}"
        pos = text.find(token, cursor)
        if pos == -1:
            continue
        facets.append(
            {
                "index": _byte_slice(text, pos, pos + len(token)),
                "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}],
            }
        )
        cursor = pos + len(token)
    return facets


class BlueskyPlatform(BasePlatform):
    name = "Bluesky"

    def is_configured(self) -> bool:
        return bool(self.settings.bluesky_handle and self.settings.bluesky_app_password)

    @property
    def _base(self) -> str:
        return self.settings.bluesky_service_url.rstrip("/")

    async def _create_session(self, client: httpx.AsyncClient, force: bool = False) -> dict:
        handle = self.settings.bluesky_handle.lstrip("@")
        cached = _session_cache.get(handle)
        if cached and not force and time.monotonic() - cached[0] < SESSION_TTL_SECONDS:
            return cached[1]

        resp = await client.post(
            f"{self._base}/xrpc/com.atproto.server.createSession",
            json={"identifier": handle, "password": self.settings.bluesky_app_password},
        )
        if resp.status_code == 401:
            raise PlatformError(f"Authentication failed: {self._error_text(resp)}")
        if resp.status_code == 429:
            raise PlatformError("Rate limit exceeded on login (createSession).")
        if resp.status_code != 200:
            raise PlatformError(f"Login failed (HTTP {resp.status_code}): {self._error_text(resp)}")

        session = resp.json()
        _session_cache[handle] = (time.monotonic(), session)
        return session

    def _build_record(self, content: PostContent) -> dict:
        text = content.message_with_hashtags(MAX_GRAPHEMES)
        record: dict = {
            "$type": "app.bsky.feed.post",
            "text": text,
            # Docs prefer a trailing "Z" over "+00:00".
            "createdAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "facets": build_facets(text, content.cta_link, content.hashtags),
        }
        if self.settings.bluesky_language:
            record["langs"] = [self.settings.bluesky_language]
        if self.settings.bluesky_link_card:
            record["embed"] = {
                "$type": "app.bsky.embed.external",
                "external": {
                    "uri": content.cta_link,
                    "title": content.title,
                    "description": content.text_without_link,
                },
            }
        return record

    async def _create_record(self, client: httpx.AsyncClient, session: dict, record: dict) -> httpx.Response:
        return await client.post(
            f"{self._base}/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
        )

    async def _publish(self, client: httpx.AsyncClient, content: PostContent) -> PublishedPost:
        session = await self._create_session(client)
        record = self._build_record(content)

        resp = await self._create_record(client, session, record)
        if resp.status_code in (400, 401) and "ExpiredToken" in resp.text:
            session = await self._create_session(client, force=True)  # cached token expired
            resp = await self._create_record(client, session, record)

        if resp.status_code == 429:
            raise PlatformError("Rate limit exceeded (createRecord).")
        if resp.status_code != 200:
            raise PlatformError(f"HTTP {resp.status_code}: {self._error_text(resp)}")

        at_uri = resp.json()["uri"]  # at://<did>/app.bsky.feed.post/<rkey>
        rkey = at_uri.rsplit("/", 1)[-1]
        return PublishedPost(
            url=f"https://bsky.app/profile/{session['handle']}/post/{rkey}",
            # bsky.app is a JS app (200 for any path), so prove visibility via the public AppView API.
            verify_url=f"{PUBLIC_APPVIEW}/xrpc/app.bsky.feed.getPostThread?uri={quote(at_uri, safe='')}&depth=0",
        )
