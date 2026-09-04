"""Strict, shared validation for HTTP API base URLs."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx


def normalize_http_base_url(value: object) -> tuple[str, str]:
    """Return a normalized absolute HTTP(S) base URL and its scheme.

    The raw spelling is checked before either URL parser sees it. This avoids
    accepting values for which ``urllib`` and ``httpx`` disagree, especially
    around leading whitespace and empty query or fragment delimiters.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("base URL is invalid")
    if "?" in value or "#" in value or "\\" in value:
        raise ValueError("base URL is invalid")
    if any(
        character.isspace() or ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise ValueError("base URL is invalid")

    normalized = value.rstrip("/")
    parsed = None
    httpx_url = None
    try:
        parsed = urlsplit(normalized)
        _ = parsed.port
        httpx_url = httpx.URL(normalized)
    except (TypeError, ValueError, httpx.InvalidURL):
        pass
    if parsed is None or httpx_url is None:
        raise ValueError("base URL is invalid")

    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("base URL is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base URL is invalid")
    if parsed.netloc.endswith(":"):
        raise ValueError("base URL is invalid")
    if not httpx_url.is_absolute_url or httpx_url.host is None or httpx_url.scheme != parsed.scheme:
        raise ValueError("base URL is invalid")
    return normalized, parsed.scheme


__all__ = ["normalize_http_base_url"]
