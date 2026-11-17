"""Request / response models and input validation."""

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, TypeAdapter, ValidationError, field_validator

MAX_MESSAGE_LENGTH = 200

# Anything that looks like a link: explicit scheme or a bare "www." host.
_LINK_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
# Punctuation that usually ends a sentence rather than belonging to the URL.
_TRAILING_PUNCTUATION = ".,;:!?)]}"
_http_url = TypeAdapter(HttpUrl)
# DNS hostname rules (RFC 1035/1123): labels of letters, digits and inner hyphens, max 63 chars.
# HttpUrl follows the WHATWG URL standard, which also accepts hosts like "exa_mple!.com".
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")  # "com", "in", or punycode IDN TLD


def _is_public_hostname(host: str) -> bool:
    """True for a real domain name (e.g. example.com); False for IPs, single labels, bad chars."""
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False  # bare IP addresses are not accepted as a CTA link
    except ValueError:
        pass
    labels = host.split(".")
    return (
        len(labels) >= 2
        and len(host) <= 253
        and all(_HOST_LABEL.match(label) for label in labels)
        and bool(_TLD.match(labels[-1]))
    )


def extract_links(text: str) -> list[str]:
    """All link-like tokens in `text`, without trailing sentence punctuation."""
    return [m.rstrip(_TRAILING_PUNCTUATION) for m in _LINK_PATTERN.findall(text)]


class PostingRequest(BaseModel):
    """Body of POST /posting. Platform selection is internal, so the message is the only input."""

    message: str = Field(
        ...,
        description=f"Message to publish (max {MAX_MESSAGE_LENGTH} characters, exactly one CTA link).",
        examples=["Check out our latest business platform and grow your online visibility. https://example.com"],
    )

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        """Rules: non-empty, <= 200 chars, exactly one link, valid http(s) URL with a real domain."""
        value = value.strip()
        if not value:
            raise ValueError("Message must not be empty.")
        if len(value) > MAX_MESSAGE_LENGTH:
            raise ValueError(f"Message must not exceed {MAX_MESSAGE_LENGTH} characters (got {len(value)}).")

        links = extract_links(value)
        if len(links) != 1:
            raise ValueError(f"Message must contain exactly one CTA link (found {len(links)}).")

        link = links[0]
        if not link.lower().startswith(("http://", "https://")):
            raise ValueError(f"CTA link must start with http:// or https:// (got '{link}').")
        try:
            url = _http_url.validate_python(link)
        except ValidationError:
            raise ValueError(f"CTA link is not a valid URL: '{link}'.") from None
        if not url.host or not _is_public_hostname(url.host):
            raise ValueError(f"CTA link must use a valid public domain name (got '{link}').")
        return value


class PlatformResult(BaseModel):
    """Outcome for one platform (fields required by the assignment)."""

    platform: str
    status: Literal["success", "failed"]
    posted_url: str | None = None
    public_access: bool = False
    date: str | None = None
    time: str | None = None
    error: str | None = None


class PostingResponse(BaseModel):
    success: bool = Field(..., description="True when at least one platform published successfully.")
    results: list[PlatformResult]


class ErrorDetail(BaseModel):
    """One input-validation problem."""

    field: str
    message: str


class ValidationErrorResponse(BaseModel):
    success: Literal[False] = False
    errors: list[ErrorDetail]
