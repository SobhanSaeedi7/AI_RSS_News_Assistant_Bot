"""RSS feed ingestion and news persistence.

Fetches RSS/Atom feeds for users, parses entries, and persists new
news items using the existing repository layer. All operations are
user-isolated: a feed or news item is only accessible to its owner.

A failure on one feed (network error, malformed content, timeout)
does not prevent other feeds from being processed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import urllib.request
import uuid
from datetime import UTC, datetime

import feedparser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_rss_news_assistant.db.models import NewsItem
from ai_rss_news_assistant.db.repository import create_news_item, list_feeds
from ai_rss_news_assistant.db.url_normalization import normalize_url
from ai_rss_news_assistant.urls import parse_http_url

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB
USER_AGENT = "AI-RSS-News-Assistant/1.0"

# Private/reserved IP ranges per RFC 1918, RFC 5737, RFC 6598, etc.
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.88.99.0/24"),  # deprecated 6to4 relay anycast
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("ff00::/8"),
]


def _is_blocked_host(hostname: str) -> bool:
    """Return True if hostname resolves to a blocked/private IP address.

    Resolves the hostname and checks each resolved IP against private,
    loopback, link-local, multicast, and reserved ranges. If the hostname
    cannot be resolved, returns False (urlopen will fail on its own).
    """
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # Not a literal IP; try DNS resolution
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            for net in _PRIVATE_NETWORKS:
                if ip in net:
                    return True
        return False

    for net in _PRIVATE_NETWORKS:
        if ip in net:
            return True
    return False


def _validate_url(url: str) -> None:
    """Validate URL scheme and host for SSRF protection.

    The scheme, hostname, and embedded-credential checks come from the shared
    URL helpers, so the bot rejects exactly the same inputs when a user adds a
    feed. Only the address blocking below is specific to outbound fetching.

    Raises ValueError if the URL is not http(s) or the host resolves to a
    blocked/private IP address.
    """
    parsed = parse_http_url(url)

    if _is_blocked_host(parsed.hostname):
        raise ValueError(f"Access to {parsed.hostname} is not allowed")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate each redirect target before urllib follows it.

    ``urlopen`` follows redirects internally, so checking only the final URL
    after the response arrives is too late: the request to the redirect target
    has already been made. This handler rejects blocked targets first.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(request: urllib.request.Request, timeout: int = REQUEST_TIMEOUT_SECONDS):
    """Open a URL using an opener that validates redirects before following."""
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    return opener.open(request, timeout=timeout)


def _safe_url_for_log(url: str) -> str:
    """Return only the hostname so credentials never reach the logs."""
    try:
        hostname = urllib.request.urlsplit(url).hostname
    except ValueError:
        return "<invalid-url>"
    return hostname or "<invalid-url>"


def _fetch_feed_content(url: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> bytes:
    """Fetch RSS/Atom feed content from a URL with a timeout.

    Uses a custom User-Agent so feed publishers can identify our crawler.
    Validates scheme and host before fetching, validates redirect targets
    before following them, and caps response size at MAX_RESPONSE_BYTES.
    """
    _validate_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _open_url(request, timeout=timeout) as response:
        # Re-validate after redirects
        final_url = response.geturl()
        if final_url != url:
            _validate_url(final_url)
        content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise ValueError(f"Response too large (max {MAX_RESPONSE_BYTES} bytes)")
        return content


def _parse_feed_content(content: bytes) -> dict:
    """Parse RSS/Atom feed content using feedparser."""
    return feedparser.parse(content)


def _extract_news_item(entry: dict) -> dict | None:
    """Extract normalized article fields from a feedparser entry.

    Returns None if the entry has no usable URL.
    """
    url = entry.get("link") or entry.get("id")
    if not url:
        return None

    title = entry.get("title") or ""

    summary = entry.get("summary") or entry.get("description") or None

    content = None
    content_list = entry.get("content")
    if isinstance(content_list, list) and len(content_list) > 0:
        content = content_list[0].get("value")
    elif isinstance(content_list, str):
        content = content_list

    published_at = None
    published_parsed = entry.get("published_parsed")
    if published_parsed:
        published_at = datetime(*published_parsed[:6], tzinfo=UTC)

    return {
        "url": url,
        "title": title,
        "content": content,
        "summary": summary,
        "published_at": published_at,
    }


async def ingest_feeds_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> dict:
    """Fetch and process all active feeds for a user.

    For each active feed owned by the user, fetches the feed, parses entries,
    and creates news items for new articles. Errors on individual feeds do
    not prevent other feeds from being processed. A per-article integrity
    conflict (e.g. duplicate normalized_url) is rolled back with a savepoint
    and does not affect other articles in the same feed.

    Returns a dict with statistics:
        feeds_processed: number of active feeds attempted
        items_created: number of new news items created
        errors: list of error dicts for failed feeds
    """
    feeds = await list_feeds(session, user_id)
    active_feeds = [f for f in feeds if f.active]

    stats: dict = {
        "feeds_processed": len(active_feeds),
        "items_created": 0,
        "errors": [],
    }

    for feed in active_feeds:
        try:
            content = await asyncio.to_thread(_fetch_feed_content, feed.url)
            parsed = await asyncio.to_thread(_parse_feed_content, content)

            entries = parsed.get("entries", [])
            for entry in entries:
                extracted = _extract_news_item(entry)
                if extracted is None:
                    continue

                try:
                    normalized_url = normalize_url(extracted["url"])
                except ValueError:
                    # A malformed article URL must not stop the rest of the feed.
                    continue

                # Skip duplicates for this user
                existing = await session.execute(
                    select(NewsItem).where(
                        NewsItem.user_id == user_id,
                        NewsItem.normalized_url == normalized_url,
                    )
                )
                if existing.scalar_one_or_none() is not None:
                    continue

                try:
                    async with session.begin_nested():
                        await create_news_item(
                            session=session,
                            user_id=user_id,
                            feed_id=feed.id,
                            url=extracted["url"],
                            title=extracted["title"],
                            normalized_url=normalized_url,
                            content=extracted.get("content"),
                            summary=extracted.get("summary"),
                            published_at=extracted.get("published_at"),
                            fetched_at=datetime.now(UTC),
                        )
                    stats["items_created"] += 1
                except IntegrityError:
                    # Savepoint was rolled back by begin_nested context manager.
                    # Continue to next article; outer transaction is intact.
                    continue

        except Exception:
            logger.exception("Failed to process feed %s", _safe_url_for_log(feed.url))
            stats["errors"].append(
                {
                    "feed_id": str(feed.id),
                    "feed_url": feed.url,
                    "error": "feed processing failed",
                }
            )

    return stats
