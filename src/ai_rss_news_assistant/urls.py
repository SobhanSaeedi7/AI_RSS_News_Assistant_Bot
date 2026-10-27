"""Safe URL helpers shared by the bot, the worker, and the agent.

Two different questions are answered here, and they are deliberately separate:

* :func:`safe_url` answers "may this stored URL be shown as a clickable link?".
  It is total: anything suspicious becomes ``None`` instead of raising.
* :func:`parse_http_url` answers "is this user-supplied feed URL acceptable?".
  It raises :class:`ValueError` with the message the bot and the ingestion
  worker already show for that input, so both reject exactly the same URLs for
  the same reasons instead of keeping two drifting copies of the rules.

Neither helper resolves DNS or inspects IP ranges. Blocking private and
reserved addresses only makes sense for outbound fetches, so that policy stays
with the fetch code in :mod:`ai_rss_news_assistant.worker.ingestion`.

The module has no dependencies beyond the standard library.
"""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit

MAX_URL_LENGTH = 2048


def safe_url(url: str | None) -> str | None:
    """Return ``url`` only when it is a safe, clickable http(s) URL."""
    if not url or len(url) > MAX_URL_LENGTH:
        return None
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return url


def parse_http_url(url: str) -> SplitResult:
    """Validate a feed URL and return its parsed parts.

    Rejects URLs whose scheme is not http(s), URLs without a hostname, and URLs
    carrying embedded userinfo: storing those would put credentials in the
    database and in logs. A malformed URL that :func:`urlsplit` itself cannot
    parse raises ``ValueError`` as well, so callers only need one except clause.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {scheme}")

    hostname = parts.hostname
    if not hostname:
        raise ValueError("URL must have a hostname")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs with embedded credentials are not allowed")

    return parts
