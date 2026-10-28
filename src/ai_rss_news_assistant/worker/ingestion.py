"""RSS feed ingestion and news persistence.

Fetches RSS/Atom feeds for users, parses entries, and persists new
news items using the existing repository layer. All operations are
user-isolated: a feed or news item is only accessible to its owner.

A failure on one feed (network error, malformed content, timeout) does not
prevent other feeds from being processed, and it is recorded on the feed so the
poll loop can back off instead of hammering a dead URL on every cycle.

Fetching, parsing, and feed classification live in
:mod:`ai_rss_news_assistant.feeds`, which the bot also uses for ``/addfeed``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_rss_news_assistant.db.models import NewsItem
from ai_rss_news_assistant.db.repository import (
    create_news_item,
    list_due_feeds,
    record_feed_failure,
    record_feed_success,
)
from ai_rss_news_assistant.db.url_normalization import normalize_url
from ai_rss_news_assistant.feeds import (
    FeedNotUsableError,
    FeedProbeStatus,
    classify_parsed_feed,
    fetch_feed_content,
    parse_feed_content,
    safe_url_for_log,
)

logger = logging.getLogger(__name__)

# Controlled backoff for a feed that keeps failing. The first failure retries
# after one poll interval, then the delay doubles up to six hours. It stays in
# the database, so a restart does not reset it.
FEED_BACKOFF_BASE_SECONDS = 300
FEED_BACKOFF_MAX_SECONDS = 6 * 60 * 60

# Fixed categories stored on the feed row. Never exception text: the column is
# shown in /listfeeds and written to logs.
ERROR_UNREACHABLE = "unreachable"
ERROR_NOT_A_FEED = "not_a_feed"
ERROR_PROCESSING = "processing"


def next_retry_delay(failures: int) -> int:
    """Return the backoff delay in seconds for a failure count."""
    if failures <= 0:
        return 0
    # Cap the exponent before shifting so a long-broken feed cannot overflow.
    exponent = min(failures - 1, 20)
    return min(FEED_BACKOFF_BASE_SECONDS * (2**exponent), FEED_BACKOFF_MAX_SECONDS)


def classify_ingestion_error(exc: BaseException) -> str:
    """Map an ingestion failure to the stored, log-safe category."""
    if isinstance(exc, FeedNotUsableError):
        return ERROR_NOT_A_FEED
    if isinstance(exc, (OSError, ValueError)):
        # urllib failures (HTTP, DNS, timeout, oversize) surface as OSError or
        # ValueError; those mean the feed could not be read this time.
        return ERROR_UNREACHABLE
    return ERROR_PROCESSING


def _extract_news_item(entry: dict) -> dict | None:
    """Extract normalized article fields from a feedparser entry.

    ``external_id`` is the publisher's stable GUID/Atom id when present. It is
    the preferred deduplication identity because two feeds can publish the same
    article under different links. The link (or the id when it is itself a URL)
    remains the display URL; an entry with neither is skipped, which is the
    safe fallback because the schema requires a URL.

    Returns None if the entry has no usable URL.
    """
    raw_external_id = entry.get("id")
    external_id = (
        raw_external_id.strip()
        if isinstance(raw_external_id, str) and raw_external_id.strip()
        else None
    )

    url = entry.get("link") or raw_external_id
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
        "external_id": external_id,
        "title": title,
        "content": content,
        "summary": summary,
        "published_at": published_at,
    }


async def _store_entries(
    session: AsyncSession, user_id: uuid.UUID, feed_id: uuid.UUID, entries: list
) -> int:
    """Persist new entries for one feed, skipping duplicates. Returns a count."""
    created = 0
    for entry in entries:
        extracted = _extract_news_item(entry)
        if extracted is None:
            continue

        try:
            normalized_url = normalize_url(extracted["url"])
        except ValueError:
            # A malformed article URL must not stop the rest of the feed.
            continue

        # Skip duplicates for this user. A stable publisher id wins when it is
        # present; the normalized URL is always checked too, so a legacy row
        # without an id still prevents a duplicate after a feed starts
        # publishing ids. Both checks are scoped to this user.
        identity_conditions = [NewsItem.normalized_url == normalized_url]
        if extracted.get("external_id"):
            identity_conditions.append(NewsItem.external_id == extracted["external_id"])
        existing = await session.execute(
            select(NewsItem).where(
                NewsItem.user_id == user_id,
                or_(*identity_conditions),
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue

        try:
            async with session.begin_nested():
                await create_news_item(
                    session=session,
                    user_id=user_id,
                    feed_id=feed_id,
                    url=extracted["url"],
                    title=extracted["title"],
                    normalized_url=normalized_url,
                    external_id=extracted.get("external_id"),
                    content=extracted.get("content"),
                    summary=extracted.get("summary"),
                    published_at=extracted.get("published_at"),
                    fetched_at=datetime.now(UTC),
                )
            created += 1
        except IntegrityError:
            # Savepoint was rolled back by begin_nested context manager.
            # Continue to next article; outer transaction is intact.
            continue

    return created


async def ingest_feeds_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> dict:
    """Fetch and process a user's due active feeds.

    Only feeds whose ``next_fetch_at`` is due are attempted, so a feed that has
    been failing is retried on an exponential schedule instead of on every
    poll. A successful fetch records ``last_fetched_at``, clears the failure
    state, and makes the feed due again immediately.

    Errors on individual feeds do not prevent other feeds from being processed.
    A per-article integrity conflict (e.g. duplicate ``normalized_url``) is
    rolled back with a savepoint and does not affect other articles in the same
    feed.

    Returns a dict with statistics:
        feeds_processed: number of due active feeds attempted
        feeds_succeeded: feeds fetched and parsed successfully
        items_created: number of new news items created
        errors: list of error dicts for failed feeds

    Active feeds still in backoff are not attempted and do not appear in
    ``feeds_processed``.
    """
    moment = now or datetime.now(UTC)
    due_feeds = await list_due_feeds(session, user_id, moment)

    stats: dict = {
        "feeds_processed": len(due_feeds),
        "feeds_succeeded": 0,
        "items_created": 0,
        "errors": [],
    }

    for feed in due_feeds:
        try:
            content = await asyncio.to_thread(fetch_feed_content, feed.url)
            parsed = await asyncio.to_thread(parse_feed_content, content)
            result = classify_parsed_feed(parsed)

            if result.status is FeedProbeStatus.NOT_A_FEED:
                # An HTML page or other non-feed: stop treating it as a feed
                # until it recovers, instead of silently producing nothing.
                raise FeedNotUsableError("response is not an RSS/Atom feed")

            # The feed's own title is authoritative; only fill it in when the
            # subscription has none, so a user-visible title never flips.
            if feed.title is None and result.title:
                feed.title = result.title

            stats["items_created"] += await _store_entries(
                session, user_id, feed.id, parsed.get("entries") or []
            )
            await record_feed_success(session, feed, moment)
            stats["feeds_succeeded"] += 1

        except Exception as exc:  # one bad feed must not stop the others
            kind = classify_ingestion_error(exc)
            delay = next_retry_delay(feed.consecutive_failures + 1)
            retry_at = moment + timedelta(seconds=delay)
            await record_feed_failure(session, feed, kind, retry_at)

            # Expected fetch failures are warnings, not tracebacks; the full
            # traceback is available at DEBUG for real bugs.
            logger.warning(
                "Feed %s failed (%s controls=%s); retrying in %ss",
                safe_url_for_log(feed.url),
                kind,
                feed.consecutive_failures,
                delay,
            )
            logger.debug("Feed failure detail", exc_info=True)

            stats["errors"].append(
                {
                    "feed_id": str(feed.id),
                    "feed_url": feed.url,
                    "error": kind,
                }
            )

    return stats
