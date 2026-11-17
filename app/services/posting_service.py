"""Publishes one message to every registered platform concurrently.

Flow:  validated message
         → ContentService   (LLM adds a title + hashtags; message itself unchanged)
         → asyncio.gather   (all platforms in parallel over one shared HTTP client)
         → PostingResponse  (one result per platform, in a stable order)

Each platform runs independently: a failure — or even an unexpected exception —
in one integration never prevents the others from posting.
"""

import asyncio
import logging
import time

import httpx

from app.config import Settings
from app.platforms import PLATFORM_CLASSES, BasePlatform
from app.schemas import PlatformResult, PostingResponse
from app.services.content_service import ContentService

logger = logging.getLogger(__name__)


class PostingService:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        platforms: list[BasePlatform] | None = None,
    ):
        self.settings = settings
        self.client = client
        self.content_service = ContentService(settings)
        # Platform selection is internal: every registered integration is used.
        self.platforms = platforms if platforms is not None else [cls(settings) for cls in PLATFORM_CLASSES]

    async def post_to_all(self, message: str) -> PostingResponse:
        started = time.perf_counter()
        content = await self.content_service.build(self.client, message)

        # Total time ≈ the slowest platform, not the sum of all of them.
        outcomes = await asyncio.gather(
            *(platform.publish(self.client, content) for platform in self.platforms),
            return_exceptions=True,
        )

        results: list[PlatformResult] = []
        for platform, outcome in zip(self.platforms, outcomes, strict=True):
            if isinstance(outcome, BaseException):  # publish() never raises; this is a last safety net
                logger.error("Unhandled error from %s: %r", platform.name, outcome)
                outcome = PlatformResult(platform=platform.name, status="failed", error="Internal error")
            results.append(outcome)

        succeeded = sum(r.status == "success" for r in results)
        logger.info(
            "Posted to %d/%d platforms in %.2fs (LLM: %s)",
            succeeded,
            len(results),
            time.perf_counter() - started,
            content.generated_by,
        )
        return PostingResponse(success=succeeded > 0, results=results)
