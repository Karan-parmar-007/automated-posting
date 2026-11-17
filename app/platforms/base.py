"""Common contract and error handling shared by every platform integration.

A platform subclass only implements `is_configured()` and `_publish()`. The public
`publish()` wrapper guarantees the rest: it never raises, converts every failure into
a `failed` PlatformResult, timestamps successes and checks public accessibility.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.config import Settings
from app.schemas import PlatformResult
from app.services.content_service import PostContent

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 300


class PlatformError(Exception):
    """Raised by an integration for an expected, human-readable failure."""


@dataclass
class PublishedPost:
    url: str  # human-facing link to the post
    is_public: bool = True  # what the platform itself reports (e.g. visibility == "public")
    # URL fetched anonymously to prove public access. Defaults to `url`; platforms whose web
    # page is a JS app that returns 200 for any path (e.g. bsky.app) point this at a public API.
    verify_url: str | None = None
    # Set when public visibility is proven by the platform API itself, so no extra HTTP probe is needed.
    verified_by_api: bool = False


class BasePlatform(ABC):
    """Base class for every integration. Subclasses set `name` and implement two methods."""

    name: str

    def __init__(self, settings: Settings):
        self.settings = settings

    @abstractmethod
    def is_configured(self) -> bool:
        """True when all required credentials are present."""

    @abstractmethod
    async def _publish(self, client: httpx.AsyncClient, content: PostContent) -> PublishedPost:
        """Publish the content and return the public URL. Raise PlatformError on failure."""

    async def publish(self, client: httpx.AsyncClient, content: PostContent) -> PlatformResult:
        """Run the integration and always return a PlatformResult — never raise."""
        if not self.is_configured():
            return self._failed("Platform not configured: missing credentials in environment.")

        try:
            post = await self._publish(client, content)
        except PlatformError as exc:
            return self._failed(str(exc))
        except httpx.TimeoutException:
            return self._failed("Request timed out.")
        except httpx.HTTPError as exc:
            return self._failed(f"Network error: {exc.__class__.__name__}: {exc}")
        except Exception as exc:
            logger.exception("Unexpected error while posting to %s", self.name)
            return self._failed(f"Unexpected error: {exc.__class__.__name__}: {exc}")

        now = datetime.now(self.settings.tz)  # posting time, captured right after the API call
        public = post.is_public and (
            post.verified_by_api or await self._is_publicly_reachable(client, post.verify_url or post.url)
        )
        return PlatformResult(
            platform=self.name,
            status="success",
            posted_url=post.url,
            public_access=public,
            date=now.strftime("%Y-%m-%d"),
            time=now.strftime("%H:%M:%S"),
        )

    async def _is_publicly_reachable(
        self, client: httpx.AsyncClient, url: str, attempts: int = 3, delay: float = 1.0
    ) -> bool:
        """Fetch the post anonymously (no auth headers) to confirm anyone can open it.

        Retries briefly on a non-200 answer because some platforms index new posts with a
        small delay; a network error (host unreachable / timeout) fails fast instead.
        """
        if not self.settings.verify_public_access:
            return True
        for attempt in range(attempts):
            try:
                resp = await client.get(url, follow_redirects=True, timeout=self.settings.public_check_timeout_seconds)
            except httpx.HTTPError:
                return False
            if resp.status_code == 200:
                return True
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
        return False

    def _failed(self, error: str) -> PlatformResult:
        # Never leak credentials (e.g. the Telegram token is part of its URL) into responses or logs.
        for secret in self.settings.secrets:
            error = error.replace(secret, "***")
        error = error[:MAX_ERROR_LENGTH]
        logger.warning("%s failed: %s", self.name, error)
        return PlatformResult(platform=self.name, status="failed", error=error)

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        """Best-effort extraction of an error message from an API response."""
        try:
            body = resp.json()
        except ValueError:
            return resp.text.strip()[:MAX_ERROR_LENGTH] or resp.reason_phrase
        if isinstance(body, dict):
            # AT Protocol style: {"error": "AuthenticationRequired", "message": "Invalid identifier or password"}
            if isinstance(body.get("error"), str) and isinstance(body.get("message"), str):
                return f"{body['error']}: {body['message']}"
            for key in ("error_description", "description", "error", "message", "detail"):
                if body.get(key):
                    return str(body[key])
        return str(body)[:MAX_ERROR_LENGTH]
