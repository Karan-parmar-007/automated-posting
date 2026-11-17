"""Telegram integration — posts to a public channel through a bot.

API docs:
  - Bot API (v10.4):  https://core.telegram.org/bots/api
  - sendMessage:      https://core.telegram.org/bots/api#sendmessage
  - MessageEntity:    https://core.telegram.org/bots/api#messageentity
  - LinkPreviewOptions: https://core.telegram.org/bots/api#linkpreviewoptions
  - Flood limits:     https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this

Auth: bot token from @BotFather. The bot must be an admin of the channel with
"Post Messages" permission.

Notes:
  - Text limit is 4096 characters.
  - Entity offsets/lengths are measured in UTF-16 code units.
  - Every response is {"ok": bool, "result" | "description", "error_code", "parameters"}.
"""

import httpx

from app.platforms.base import BasePlatform, PlatformError, PublishedPost
from app.services.content_service import PostContent

API_BASE = "https://api.telegram.org"
MAX_TEXT_LENGTH = 4096


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def build_text_and_entities(content: PostContent, include_title: bool) -> tuple[str, list[dict]]:
    """Plain text plus explicit entities (bold title, URL, hashtags) — no Markdown escaping needed."""
    body = content.message_with_hashtags(MAX_TEXT_LENGTH)
    prefix = ""
    entities: list[dict] = []
    if include_title and content.generated_by != "fallback":  # fallback title just repeats the message
        candidate = f"{content.title}\n\n"
        if len(candidate) + len(body) <= MAX_TEXT_LENGTH:
            prefix = candidate
            entities.append({"type": "bold", "offset": 0, "length": _utf16_len(content.title)})
    text = prefix + body

    def add(entity_type: str, needle: str, start_at: int) -> int:
        pos = text.find(needle, start_at)
        if pos == -1:
            return start_at
        entities.append({"type": entity_type, "offset": _utf16_len(text[:pos]), "length": _utf16_len(needle)})
        return pos + len(needle)

    cursor = add("url", content.cta_link, len(prefix))
    for tag in content.hashtags:
        cursor = add("hashtag", f"#{tag}", cursor)
    return text, entities


class TelegramPlatform(BasePlatform):
    name = "Telegram"

    def is_configured(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_channel)

    async def _publish(self, client: httpx.AsyncClient, content: PostContent) -> PublishedPost:
        text, entities = build_text_and_entities(content, self.settings.telegram_include_title)
        channel = self.settings.telegram_channel.strip()
        if not channel.startswith("@") and not channel.lstrip("-").isdigit():
            channel = f"@{channel}"

        resp = await client.post(
            f"{API_BASE}/bot{self.settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": channel,
                "text": text,
                "entities": entities,
                "link_preview_options": {"url": content.cta_link, "prefer_large_media": True},
            },
        )
        try:
            data = resp.json()
        except ValueError:
            raise PlatformError(f"HTTP {resp.status_code}: non-JSON response") from None

        if not data.get("ok"):
            code = data.get("error_code", resp.status_code)
            description = data.get("description", "Unknown error")
            if code == 401:
                raise PlatformError(f"Authentication failed: {description} (check TELEGRAM_BOT_TOKEN)")
            if code == 429:
                retry_after = data.get("parameters", {}).get("retry_after", "?")
                raise PlatformError(f"Rate limit exceeded: retry after {retry_after}s.")
            raise PlatformError(f"Error {code}: {description}")

        message = data["result"]
        chat = message["chat"]
        message_id = message["message_id"]
        username = chat.get("username")
        if username:
            # Public channel: anyone can open t.me/<username>/<id>. t.me itself is blocked or
            # unreliable on many networks, so visibility is derived from the API response.
            return PublishedPost(
                url=f"https://t.me/{username}/{message_id}",
                is_public=chat.get("type") == "channel",
                verified_by_api=True,
            )
        # Private channel: link only works for members.
        internal_id = str(chat["id"]).removeprefix("-100")
        return PublishedPost(url=f"https://t.me/c/{internal_id}/{message_id}", is_public=False, verified_by_api=True)
