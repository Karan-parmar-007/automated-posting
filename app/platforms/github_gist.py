"""GitHub Gist integration — publishes the message as a public Markdown gist.

API docs:
  - Create a gist:   https://docs.github.com/en/rest/gists/gists#create-a-gist
  - API versions:    https://docs.github.com/en/rest/about-the-rest-api/api-versions
  - Rate limits:     https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api

Auth: fine-grained personal access token with account permission
"Gists: Read and write" (or a classic token with the `gist` scope).
"""

import re
from datetime import UTC, datetime

import httpx

from app.platforms.base import BasePlatform, PlatformError, PublishedPost
from app.services.content_service import PostContent

API_BASE = "https://api.github.com"


def _slug(text: str, max_length: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "post"


def build_gist(content: PostContent) -> tuple[str, str, str]:
    """Return (filename, description, markdown body)."""
    filename = f"{_slug(content.title)}.md"
    lines = [
        f"# {content.title}",
        "",
        content.message,
        "",
        f"**👉 [{content.cta_link}]({content.cta_link})**",
    ]
    if content.hashtags:
        lines += ["", " ".join(f"`#{t}`" for t in content.hashtags)]
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines += ["", "---", f"<sub>Published {stamp} by automated-posting</sub>", ""]
    return filename, content.title, "\n".join(lines)


class GitHubGistPlatform(BasePlatform):
    name = "GitHub Gist"

    def is_configured(self) -> bool:
        return bool(self.settings.github_token)

    async def _publish(self, client: httpx.AsyncClient, content: PostContent) -> PublishedPost:
        filename, description, body = build_gist(content)
        resp = await client.post(
            f"{API_BASE}/gists",
            headers={
                "Authorization": f"Bearer {self.settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.settings.github_api_version,
            },
            json={"description": description, "public": True, "files": {filename: {"content": body}}},
        )

        if resp.status_code == 401:
            raise PlatformError(f"Authentication failed: {self._error_text(resp)} (check GITHUB_TOKEN)")
        if resp.status_code in (403, 429):
            if resp.headers.get("x-ratelimit-remaining") == "0" or resp.status_code == 429:
                reset = resp.headers.get("x-ratelimit-reset", "unknown")
                raise PlatformError(f"Rate limit exceeded (resets at epoch {reset}).")
            raise PlatformError(f"Forbidden: {self._error_text(resp)} (token needs 'Gists: Read and write' permission)")
        if resp.status_code != 201:
            raise PlatformError(f"HTTP {resp.status_code}: {self._error_text(resp)}")

        gist = resp.json()
        return PublishedPost(url=gist["html_url"], is_public=bool(gist.get("public")))
