"""FastAPI entrypoint.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 4000
Docs: http://localhost:4000/docs
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import Settings, get_settings
from app.schemas import ErrorDetail, PostingRequest, PostingResponse, ValidationErrorResponse
from app.services.posting_service import PostingService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO level — the Telegram bot token is part of its URL.
logging.getLogger("httpx").setLevel(logging.WARNING)

USER_AGENT = "automated-posting/1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """One pooled HTTP client for the app's lifetime: connections (TLS) are reused across requests."""
    settings = get_settings()
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    ) as client:
        app.state.http_client = client
        yield


app = FastAPI(
    title="Automated Multi-Platform Posting System",
    description=(
        "Publishes a short message (max 200 characters, exactly one CTA link) to Mastodon, "
        "Bluesky, Telegram, GitHub Gist and DEV Community through their official APIs. "
        "An LLM (Gemini, with local Ollama as fallback) adds a title and hashtags."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return clean `{success: false, errors: [{field, message}]}` instead of FastAPI's raw format."""
    errors = [
        ErrorDetail(
            field=".".join(str(p) for p in err["loc"] if p != "body") or "body",
            message=err["msg"].removeprefix("Value error, "),
        )
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content=ValidationErrorResponse(errors=errors).model_dump())


def get_posting_service(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> PostingService:
    return PostingService(settings, request.app.state.http_client)


@app.post(
    "/posting",
    response_model=PostingResponse,
    responses={422: {"model": ValidationErrorResponse, "description": "Invalid input"}},
    summary="Publish a message to all supported platforms",
)
async def posting(
    payload: PostingRequest,
    service: Annotated[PostingService, Depends(get_posting_service)],
) -> PostingResponse:
    """Validates the message, then publishes it to every platform concurrently.

    Always returns 200 with one result per platform once validation passes;
    `success` is true when at least one platform published.
    """
    return await service.post_to_all(payload.message)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}
