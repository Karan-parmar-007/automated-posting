"""DEV Community (dev.to) integration — publishes the message as a short article.

API docs (Forem API v1):
  - Publish article:  https://developers.forem.com/api/v1#tag/articles/operation/createArticle
  - Authentication:   https://developers.forem.com/api/v1#section/Authentication

Auth: API key from https://dev.to/settings/extensions, sent in the `api-key` header.

Notes:
  - `published` defaults to false (draft), so it is set explicitly.
  - Up to 4 tags, sent as a comma-separated string; DEV tags are lowercase alphanumeric.
"""

import re

import httpx

from app.platforms.base import BasePlatform, PlatformError, PublishedPost
from app.services.content_service import PostContent

API_URL = "https://dev.to/api/articles"
MAX_TAGS = 4
MAX_TAG_LENGTH = 30


def to_devto_tags(hashtags: list[str]) -> str:
    """CamelCase hashtags -> DEV tags: lowercase, alphanumeric, unique, max 4."""
    tags: list[str] = []
    for tag in hashtags:
        tag = re.sub(r"[^a-z0-9]", "", tag.lower())[:MAX_TAG_LENGTH]
        if tag and tag not in tags:
            tags.append(tag)
    return ",".join(tags[:MAX_TAGS])


def build_article(content: PostContent) -> dict:
    body = "\n".join(
        [
            content.message,
            "",
            f"👉 **[{content.cta_link}]({content.cta_link})**",
        ]
    )
    article = {
        "title": content.title,
        "body_markdown": body,
        "published": True,
        "description": content.text_without_link[:150],
    }
    if tags := to_devto_tags(content.hashtags):
        article["tags"] = tags
    return {"article": article}


class DevToPlatform(BasePlatform):
    name = "DEV Community"

    def is_configured(self) -> bool:
        return bool(self.settings.devto_api_key)

    async def _publish(self, client: httpx.AsyncClient, content: PostContent) -> PublishedPost:
        resp = await client.post(
            API_URL,
            headers={
                "api-key": self.settings.devto_api_key,
                "Accept": "application/vnd.forem.api-v1+json",
            },
            json=build_article(content),
        )

        if resp.status_code == 401:
            raise PlatformError(f"Authentication failed: {self._error_text(resp)} (check DEVTO_API_KEY)")
        if resp.status_code == 429:
            retry = resp.headers.get("retry-after", "?")
            raise PlatformError(f"Rate limit exceeded: retry after {retry}s.")
        if resp.status_code != 201:
            raise PlatformError(f"HTTP {resp.status_code}: {self._error_text(resp)}")

        article = resp.json()
        return PublishedPost(url=article["url"], is_public=bool(article.get("published", True)))
