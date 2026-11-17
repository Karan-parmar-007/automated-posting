"""Builds the content each platform publishes.

The user's message (with its CTA link) is always published verbatim. An LLM only
adds *metadata* — a short title and a few hashtags — that platforms can use (e.g.
hashtags on Mastodon/Bluesky, a title for link cards and article platforms).
Because the LLM never rewrites the message, it can never break or change the link.

Providers are tried in the order of LLM_PROVIDERS (default: Gemini, then local
Ollama). If every provider fails we fall back to a title derived from the message
and no hashtags — posting always continues.

Docs:
  - Gemini generateContent: https://ai.google.dev/api/generate-content
  - Gemini structured output: https://ai.google.dev/gemini-api/docs/structured-output
  - Ollama chat API:          https://docs.ollama.com/api/chat
  - Ollama structured output: https://docs.ollama.com/capabilities/structured-outputs
"""

import json
import logging
import re
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.schemas import extract_links

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 80
MAX_HASHTAGS = 4
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

SYSTEM_PROMPT = (
    "You write metadata for a short social media post. Do not rewrite the post.\n"
    "Return JSON matching this schema: "
    '{"title": string, "hashtags": [string]}\n'
    f"- title: a catchy headline, max {MAX_TITLE_LENGTH - 10} characters, no URLs, no hashtags, no emojis.\n"
    f"- hashtags: 2 to {MAX_HASHTAGS} relevant hashtags in CamelCase, letters/digits only, without the # sign."
)

Provider = Literal["gemini", "ollama", "fallback"]


class LLMError(Exception):
    """An LLM provider could not produce usable metadata (the next provider is tried)."""


class LLMMetadata(BaseModel):
    """Schema the LLM output is constrained to (sent to the provider as a JSON schema)."""

    title: str
    hashtags: list[str] = Field(default_factory=list)


# Built once at import time instead of on every request.
LLM_JSON_SCHEMA = LLMMetadata.model_json_schema()


class PostContent(BaseModel):
    """Everything a platform needs to publish one post."""

    message: str  # original, validated message — always published unchanged
    cta_link: str
    title: str
    hashtags: list[str] = Field(default_factory=list)
    generated_by: Provider = "fallback"

    @property
    def text_without_link(self) -> str:
        """Message text minus the CTA link (used for descriptions / link cards)."""
        return strip_link(self.message, self.cta_link)

    def message_with_hashtags(self, max_length: int) -> str:
        """Message plus as many hashtags as fit within `max_length` characters."""
        tags = [f"#{t}" for t in self.hashtags]
        while tags:
            candidate = f"{self.message}\n\n{' '.join(tags)}"
            if len(candidate) <= max_length:
                return candidate
            tags.pop()
        return self.message


def strip_link(message: str, link: str) -> str:
    """Message text with the link removed and whitespace/dangling separators tidied."""
    return _WHITESPACE.sub(" ", message.replace(link, "")).strip(" -–—:|")


def fallback_title(message: str, link: str) -> str:
    """Title used when no LLM is available: the message text without its link, word-trimmed."""
    text = strip_link(message, link)
    if len(text) <= MAX_TITLE_LENGTH:
        return text or "New update"
    cut = text[: MAX_TITLE_LENGTH - 1].rsplit(" ", 1)[0]
    return f"{cut}…"


def _clean_title(title: str) -> str | None:
    """Normalised title, or None if the LLM output is unusable (empty, too long, contains a URL)."""
    title = _WHITESPACE.sub(" ", title).strip().strip("\"'")
    if not title or extract_links(title) or len(title) > MAX_TITLE_LENGTH:
        return None
    return title


def _clean_hashtags(tags: list[str]) -> list[str]:
    """Keep letters/digits only, drop duplicates (case-insensitive), cap at MAX_HASHTAGS."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        tag = _NON_ALNUM.sub("", tag)
        if tag.isupper() and len(tag) > 4:  # "WEBDEVELOPMENT" -> "Webdevelopment"
            tag = tag.capitalize()
        key = tag.lower()
        if 2 <= len(tag) <= 30 and not tag.isdigit() and key not in seen:
            seen.add(key)
            cleaned.append(tag)
            if len(cleaned) == MAX_HASHTAGS:
                break
    return cleaned


class ContentService:
    """Turns a validated message into PostContent, enriched by the first LLM that succeeds."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._providers = {"gemini": self._ask_gemini, "ollama": self._ask_ollama}

    async def build(self, client: httpx.AsyncClient, message: str) -> PostContent:
        link = extract_links(message)[0]
        content = PostContent(message=message, cta_link=link, title=fallback_title(message, link))
        if not self.settings.llm_enabled:
            return content

        for name in self.settings.llm_provider_list:
            ask = self._providers.get(name)
            if ask is None:
                logger.warning("Unknown LLM provider %r — skipped", name)
                continue
            try:
                meta = await ask(client, message)
            except (LLMError, httpx.HTTPError, ValueError) as exc:  # LLM problems must never block posting
                logger.warning("LLM provider %s failed: %s: %s", name, exc.__class__.__name__, exc)
                continue

            content.title = _clean_title(meta.title) or content.title
            content.hashtags = _clean_hashtags(meta.hashtags)
            content.generated_by = name  # type: ignore[assignment]
            logger.info("LLM %s metadata: title=%r hashtags=%s", name, content.title, content.hashtags)
            return content

        logger.warning("No LLM provider succeeded; publishing without enrichment.")
        return content

    async def _ask_gemini(self, client: httpx.AsyncClient, message: str) -> LLMMetadata:
        """Gemini generateContent with a JSON schema (responseJsonSchema) for structured output."""
        if not self.settings.gemini_api_key:
            raise LLMError("GEMINI_API_KEY not set")
        generation_config: dict = {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseJsonSchema": LLM_JSON_SCHEMA,
        }
        if self.settings.gemini_thinking_level:
            generation_config["thinkingConfig"] = {"thinkingLevel": self.settings.gemini_thinking_level}

        resp = await client.post(
            f"{GEMINI_API}/models/{self.settings.gemini_model}:generateContent",
            headers={"x-goog-api-key": self.settings.gemini_api_key},
            json={
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": message}]}],
                "generationConfig": generation_config,
            },
            timeout=self.settings.gemini_timeout_seconds,
        )
        if resp.status_code != 200:
            try:
                detail = resp.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                detail = resp.text[:200]
            raise LLMError(f"HTTP {resp.status_code}: {detail}")

        data = resp.json()
        try:
            raw = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError):
            reason = data.get("promptFeedback", {}).get("blockReason") or "no candidates"
            raise LLMError(f"Gemini returned no content ({reason})") from None
        return self._parse(raw)

    async def _ask_ollama(self, client: httpx.AsyncClient, message: str) -> LLMMetadata:
        """Local Ollama /api/chat with `format` set to the JSON schema (structured output)."""
        resp = await client.post(
            f"{self.settings.ollama_base_url.rstrip('/')}/api/chat",
            json={
                "model": self.settings.ollama_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": message},
                ],
                "format": LLM_JSON_SCHEMA,  # structured output: reply is constrained to this schema
                "stream": False,
                "think": False,  # Qwen 3.5 is a thinking model; we only need the JSON
                "keep_alive": self.settings.ollama_keep_alive,
                "options": {"temperature": 0.2},
            },
            timeout=self.settings.ollama_timeout_seconds,
        )
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return self._parse(resp.json().get("message", {}).get("content", ""))

    @staticmethod
    def _parse(raw: str) -> LLMMetadata:
        try:
            return LLMMetadata.model_validate_json(raw)
        except ValidationError as exc:
            raise LLMError(f"LLM returned invalid JSON: {json.dumps(raw)[:200]}") from exc
