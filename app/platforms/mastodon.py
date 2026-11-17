"""Mastodon integration.

API docs:
  - Post a new status: https://docs.joinmastodon.org/methods/statuses/#create
  - Rate limits:       https://docs.joinmastodon.org/api/rate-limits/

Auth: user access token with the `write:statuses` scope
(Preferences → Development → New application).
"""

import uuid

import httpx

from app.platforms.base import BasePlatform, PlatformError, PublishedPost
from app.services.content_service import PostContent


class MastodonPlatform(BasePlatform):
    name = "Mastodon"

    def is_configured(self) -> bool:
        return bool(self.settings.mastodon_base_url and self.settings.mastodon_access_token)

    async def _publish(self, client: httpx.AsyncClient, content: PostContent) -> PublishedPost:
        base_url = self.settings.mastodon_base_url.rstrip("/")
        # Mastodon links hashtags automatically; add as many LLM tags as fit the limit.
        status_text = content.message_with_hashtags(self.settings.mastodon_max_characters)
        data = {"status": status_text, "visibility": self.settings.mastodon_visibility}
        if self.settings.mastodon_language:
            data["language"] = self.settings.mastodon_language

        resp = await client.post(
            f"{base_url}/api/v1/statuses",
            headers={
                "Authorization": f"Bearer {self.settings.mastodon_access_token}",
                # Server de-duplicates requests with the same key for 1 hour.
                "Idempotency-Key": str(uuid.uuid4()),
            },
            data=data,
        )

        if resp.status_code == 401:
            raise PlatformError(f"Authentication failed: {self._error_text(resp)}")
        if resp.status_code == 429:
            reset = resp.headers.get("X-RateLimit-Reset", "unknown")
            raise PlatformError(f"Rate limit exceeded (resets at {reset}).")
        if resp.status_code != 200:
            raise PlatformError(f"HTTP {resp.status_code}: {self._error_text(resp)}")

        status = resp.json()
        url = status.get("url") or status.get("uri")
        if not url:
            raise PlatformError("Status created but the response contained no URL.")
        return PublishedPost(url=url, is_public=status.get("visibility") == "public")
