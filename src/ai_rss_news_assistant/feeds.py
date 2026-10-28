"""Feed fetching, URL safety, and feed-validity probing.

This module is shared infrastructure in the same sense as
:mod:`ai_rss_news_assistant.urls`: it owns no service-specific policy, but both
the bot and the worker need the same answers, and keeping two copies would let
them drift.

Two entry points matter:

:func:`probe_feed`
    Answers "is this user-supplied URL a usable RSS/Atom feed?" for the bot, so
    ``/addfeed`` can reject an unreachable server or an HTML page instead of
    storing something that will never produce news. It returns a
    :class:`FeedProbe` and never raises.

:func:`fetch_feed_content` / :func:`parse_feed_content`
    The polling path. The worker fetches once, parses once, and classifies the
    result with :func:`classify_parsed_feed`, so a poll never fetches the same
    feed twice.

Security notes:

* :func:`validate_feed_url` rejects non-http(s) schemes, missing hostnames,
  embedded credentials, and hosts that resolve into private, loopback,
  link-local, multicast, or reserved ranges. It runs before every fetch *and*
  before every redirect target, so a public URL cannot redirect the crawler
  into the internal network.
* Fetching is bounded by a timeout and by a maximum response size.
* :func:`safe_url_for_log` returns only the hostname, so a URL carrying
  credentials or a token in its path never reaches a log line.

Residual risk: the address check resolves DNS and ``urllib`` resolves again
when it connects, so a hostile DNS server could answer differently the second
time (DNS rebinding). Closing that fully needs connection-level pinning; it is
documented as a limitation rather than silently assumed away.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import urllib.request
from dataclasses import dataclass
from enum import Enum
from urllib.parse import SplitResult

import feedparser

from ai_rss_news_assistant.urls import parse_http_url

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB
MAX_TITLE_CHARS = 200
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


class BlockedDestinationError(ValueError):
    """A URL points at a private, loopback, or reserved destination.

    It subclasses :class:`ValueError` so existing callers that already treat an
    invalid URL as ``ValueError`` keep working; the bot maps it to a distinct,
    deliberately vague user message.
    """


def is_blocked_host(hostname: str) -> bool:
    """Return True if ``hostname`` resolves to a blocked/private IP address.

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


def validate_feed_url(url: str) -> SplitResult:
    """Validate scheme, host, and destination before any outbound request.

    Raises :class:`ValueError` for a malformed or non-http(s) URL and
    :class:`BlockedDestinationError` for a blocked address.
    """
    parsed = parse_http_url(url)

    if is_blocked_host(parsed.hostname):
        raise BlockedDestinationError(f"Access to {parsed.hostname} is not allowed")

    return parsed


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate each redirect target before urllib follows it.

    ``urlopen`` follows redirects internally, so checking only the final URL
    after the response arrives is too late: the request to the redirect target
    has already been made. This handler rejects blocked targets first.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_feed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(request: urllib.request.Request, timeout: int = REQUEST_TIMEOUT_SECONDS):
    """Open a URL using an opener that validates redirects before following."""
    opener = urllib.request.build_opener(SafeRedirectHandler())
    return opener.open(request, timeout=timeout)


def safe_url_for_log(url: str) -> str:
    """Return only the hostname so credentials never reach the logs."""
    try:
        hostname = urllib.request.urlsplit(url).hostname
    except ValueError:
        return "<invalid-url>"
    return hostname or "<invalid-url>"


def fetch_feed_content(url: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> bytes:
    """Fetch RSS/Atom (or article) content with a timeout and a size cap.

    Uses a custom User-Agent so publishers can identify our crawler. Validates
    scheme and host before fetching, validates redirect targets before
    following them, and caps the response at :data:`MAX_RESPONSE_BYTES`.
    """
    validate_feed_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _open_url(request, timeout=timeout) as response:
        # Re-validate after redirects
        final_url = response.geturl()
        if final_url != url:
            validate_feed_url(final_url)
        content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise ValueError(f"Response too large (max {MAX_RESPONSE_BYTES} bytes)")
        return content


def parse_feed_content(content: bytes) -> dict:
    """Parse RSS/Atom content with feedparser."""
    return feedparser.parse(content)


class FeedProbeStatus(Enum):
    """Outcome of asking "is this URL a usable feed?"."""

    VALID = "valid"
    """A feed with at least one entry."""

    EMPTY = "empty"
    """A structurally valid feed that currently has no entries."""

    NOT_A_FEED = "not_a_feed"
    """The response was not RSS/Atom at all (an HTML page, JSON, plain text)."""

    UNREACHABLE = "unreachable"
    """DNS, connection, TLS, HTTP status, or timeout failure."""

    BLOCKED = "blocked"
    """The destination is private/reserved, or the URL is unusable."""

    INVALID = "invalid"
    """The URL is not a valid http(s) URL."""


@dataclass(frozen=True)
class FeedProbe:
    """Result of probing one feed URL."""

    status: FeedProbeStatus
    title: str | None = None
    entries: int = 0

    @property
    def is_usable(self) -> bool:
        """True when the URL may be stored as a feed."""
        return self.status in (FeedProbeStatus.VALID, FeedProbeStatus.EMPTY)


class FeedNotUsableError(ValueError):
    """Raised when a response is not a usable RSS/Atom document."""


def _clean_title(value: object) -> str | None:
    """Collapse and bound a feed title, or return None when there is none."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:MAX_TITLE_CHARS]


def classify_parsed_feed(parsed: object) -> FeedProbe:
    """Classify an already-parsed feed without fetching anything again.

    feedparser is deliberately forgiving: it sets ``bozo`` for recoverable
    problems (a bad date, one broken entry, a mismatched encoding) and *still*
    returns usable entries. Rejecting every ``bozo`` document would reject real
    feeds, so acceptance is decided by whether the document actually looks like
    a feed:

    * no declared feed version and no entries -> not a feed;
    * at least one entry -> valid;
    * a declared version but no entries -> a valid, currently empty feed.

    ``bozo`` is logged for diagnosis but never decides the outcome on its own.
    """
    document = parsed if isinstance(parsed, dict) else {}
    entries = document.get("entries") or []
    if not isinstance(entries, list):
        entries = []

    version = document.get("version") or ""
    if not isinstance(version, str):
        version = ""

    feed_meta = document.get("feed") or {}
    title = (
        _clean_title(feed_meta.get("title")) if isinstance(feed_meta, dict) else None
    )

    if document.get("bozo"):
        logger.debug(
            "Feed parsed with recoverable warnings: %s",
            document.get("bozo_exception"),
        )

    if not version.strip() and not entries:
        return FeedProbe(FeedProbeStatus.NOT_A_FEED)

    if entries:
        return FeedProbe(FeedProbeStatus.VALID, title=title, entries=len(entries))

    return FeedProbe(FeedProbeStatus.EMPTY, title=title, entries=0)


def probe_feed(url: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> FeedProbe:
    """Fetch and classify one URL without ever raising.

    This is the add-time check behind ``/addfeed``. It performs exactly one
    request; the worker performs its own fetch on the next poll.

    The call is blocking, so async callers must run it with
    :func:`asyncio.to_thread` to keep the event loop free.
    """
    try:
        validate_feed_url(url)
    except BlockedDestinationError:
        return FeedProbe(FeedProbeStatus.BLOCKED)
    except ValueError:
        return FeedProbe(FeedProbeStatus.INVALID)

    try:
        content = fetch_feed_content(url, timeout)
    except BlockedDestinationError:
        # A redirect pointed somewhere it must not go.
        logger.warning("Blocked a redirect while probing a feed")
        return FeedProbe(FeedProbeStatus.BLOCKED)
    except Exception:  # noqa: BLE001 - every fetch failure is "unreachable"
        logger.info(
            "Feed probe failed for %s (unreachable or rejected)",
            safe_url_for_log(url),
        )
        return FeedProbe(FeedProbeStatus.UNREACHABLE)

    try:
        parsed = parse_feed_content(content)
    except Exception:  # noqa: BLE001 - a parser crash means unusable input
        logger.info("Feed probe could not parse %s", safe_url_for_log(url))
        return FeedProbe(FeedProbeStatus.NOT_A_FEED)

    return classify_parsed_feed(parsed)


async def probe_feed_async(
    url: str, timeout: int = REQUEST_TIMEOUT_SECONDS
) -> FeedProbe:
    """Run :func:`probe_feed` off the event loop."""
    return await asyncio.to_thread(probe_feed, url, timeout)
