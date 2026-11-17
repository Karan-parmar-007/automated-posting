from app.platforms.base import BasePlatform, PlatformError, PublishedPost
from app.platforms.bluesky import BlueskyPlatform
from app.platforms.devto import DevToPlatform
from app.platforms.github_gist import GitHubGistPlatform
from app.platforms.mastodon import MastodonPlatform
from app.platforms.telegram import TelegramPlatform

# Every platform the service publishes to. Add new integrations here.
PLATFORM_CLASSES: list[type[BasePlatform]] = [
    MastodonPlatform,
    BlueskyPlatform,
    TelegramPlatform,
    GitHubGistPlatform,
    DevToPlatform,
]

__all__ = ["PLATFORM_CLASSES", "BasePlatform", "PlatformError", "PublishedPost"]
