"""Application settings, loaded from environment variables and `.env`.

Every credential is optional: a platform without credentials is reported as
"not configured" in the response instead of crashing the app.
"""

from functools import cached_property, lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

KNOWN_LLM_PROVIDERS = {"gemini", "ollama"}


class Settings(BaseSettings):
    # Env var names are case-insensitive (GEMINI_API_KEY == gemini_api_key).
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General -------------------------------------------------------------
    http_timeout_seconds: float = 15.0
    timezone: str = "UTC"  # timezone of the date/time reported per platform
    verify_public_access: bool = True  # fetch each posted URL anonymously to confirm it is public
    public_check_timeout_seconds: float = 8.0

    # --- LLM enrichment --------------------------------------------------------
    # Adds a title + hashtags to each post; the message itself is never changed.
    # Providers are tried in order; if all fail, posting continues without enrichment.
    llm_enabled: bool = True
    llm_providers: str = "gemini,ollama"

    # Google Gemini — https://ai.google.dev/api/generate-content
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_thinking_level: str | None = "minimal"  # empty = model default
    gemini_timeout_seconds: float = 10.0

    # Ollama (local) — https://docs.ollama.com/api/chat
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:4b-q4_K_M"
    ollama_timeout_seconds: float = 60.0  # first call may need to load the model into memory
    ollama_keep_alive: str = "10m"

    # --- Platforms -------------------------------------------------------------
    # Mastodon — https://docs.joinmastodon.org/methods/statuses/#create
    mastodon_base_url: str = "https://mastodon.social"
    mastodon_access_token: str | None = None
    mastodon_visibility: str = "public"
    mastodon_language: str | None = "en"
    mastodon_max_characters: int = 500  # instance limit (GET /api/v2/instance)

    # Bluesky (AT Protocol) — https://docs.bsky.app/docs/advanced-guides/posts
    bluesky_service_url: str = "https://bsky.social"
    bluesky_handle: str | None = None
    bluesky_app_password: str | None = None
    bluesky_language: str | None = "en"
    bluesky_link_card: bool = True  # attach an app.bsky.embed.external card for the CTA link

    # Telegram Bot API — https://core.telegram.org/bots/api#sendmessage
    telegram_bot_token: str | None = None
    telegram_channel: str | None = None  # public channel, e.g. @my_channel (bot must be admin)
    telegram_include_title: bool = True  # bold LLM title above the message

    # GitHub Gists — https://docs.github.com/en/rest/gists/gists#create-a-gist
    github_token: str | None = None  # fine-grained PAT with "Gists: read and write" (or classic `gist` scope)
    github_api_version: str = "2026-03-10"

    # DEV Community (Forem API v1) — https://developers.forem.com/api/v1
    devto_api_key: str | None = None  # https://dev.to/settings/extensions

    # --- Validation (fail fast at startup instead of on every request) ---------
    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"Unknown timezone '{value}' (use an IANA name like 'Asia/Kolkata').") from None
        return value

    @field_validator("llm_providers")
    @classmethod
    def _valid_providers(cls, value: str) -> str:
        unknown = {p.strip().lower() for p in value.split(",") if p.strip()} - KNOWN_LLM_PROVIDERS
        if unknown:
            raise ValueError(f"Unknown LLM provider(s): {', '.join(sorted(unknown))}.")
        return value

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @cached_property
    def llm_provider_list(self) -> list[str]:
        return [p.strip().lower() for p in self.llm_providers.split(",") if p.strip()]

    @cached_property
    def secrets(self) -> list[str]:
        """All configured credentials — used to scrub them from error messages."""
        values = (
            self.gemini_api_key,
            self.mastodon_access_token,
            self.bluesky_app_password,
            self.telegram_bot_token,
            self.github_token,
            self.devto_api_key,
        )
        return [v for v in values if v]


@lru_cache
def get_settings() -> Settings:
    """Settings are read once per process and reused (FastAPI dependency)."""
    return Settings()
