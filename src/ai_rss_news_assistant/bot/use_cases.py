"""Bot use cases: feed management, delivery preference, news, and questions.

Telegram handlers translate an update into a call here and render the returned
outcome; they contain no orchestration of their own. Keeping the logic in this
module means it can be tested without Telegram objects and that ownership
scoping stays in one place: every query is issued through repository functions
that take the internal ``user_id``, so a handler can never widen another user's
data.

The question-answering use case depends on the :class:`Answerer` protocol --
``answer(user_id, question)`` -- rather than on the agent implementation, which
is the boundary the HTTP client also satisfies.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from ai_rss_news_assistant.config import DEFAULT_FETCH_TIMEOUT_SECONDS
from ai_rss_news_assistant.contracts import AGENT_ERROR, AgentAnswer
from ai_rss_news_assistant.db import repository
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.db.models import NewsItem, UserFeed
from ai_rss_news_assistant.db.repository import (
    acquire_agent_lease,
    create_feed,
    create_user,
    delete_feed,
    get_user_by_telegram_id,
    list_feeds,
    list_news_items_with_feed,
    release_agent_lease,
    toggle_auto_delivery,
    update_user_settings,
)
from ai_rss_news_assistant.db.url_normalization import normalize_url
from ai_rss_news_assistant.feeds import FeedProbeStatus, probe_feed_async
from ai_rss_news_assistant.urls import parse_http_url

logger = logging.getLogger(__name__)

# Unique constraint on (user_id, url); used to tell a real duplicate apart from
# another integrity failure, which must not be reported as "already added".
_FEED_UNIQUE_CONSTRAINT = "uq_user_feeds_user_id_url"

# The bot holds a per-user lease while an /ask is being answered. It is a
# coordination hint for the worker, not a lock: it expires on its own so a
# crashed bot cannot silence a user's automatic delivery forever. The value is
# comfortably longer than the agent HTTP timeout (40 s by default) and short
# enough that a restart resumes delivery promptly.
AGENT_LEASE_SECONDS = 120


class Answerer(Protocol):
    """Anything that can answer a question for one internal user id."""

    async def answer(self, user_id: uuid.UUID, question: str) -> AgentAnswer:
        """Return an answer drawn only from that user's stored news."""
        ...


class FeedAddStatus(Enum):
    """Outcome of an /addfeed request.

    ``INVALID`` covers a URL that is not a usable http(s) URL at all;
    ``BLOCKED`` a private/reserved destination; ``UNREACHABLE`` a fetch that
    failed; ``NOT_A_FEED`` a response that is not RSS/Atom. Only ``CREATED``,
    ``EMPTY`` and ``DUPLICATE`` are terminal states of an existing or new
    subscription.
    """

    CREATED = "created"
    EMPTY = "empty"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    BLOCKED = "blocked"
    UNREACHABLE = "unreachable"
    NOT_A_FEED = "not_a_feed"


@dataclass(frozen=True)
class FeedAddOutcome:
    """Result of adding a feed, with the feed when it was created."""

    status: FeedAddStatus
    feed: UserFeed | None = None


async def ensure_user_id(db: Database, telegram_user_id: int) -> uuid.UUID:
    """Return the internal id of the Telegram user, creating the row if needed."""
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)
        return user.id


def _is_duplicate_feed_violation(exc: IntegrityError) -> bool:
    """Check whether an IntegrityError is a unique violation on (user_id, url).

    PostgreSQL/asyncpg reports unique violations with SQLSTATE 23505 and
    exposes the violated constraint name on the underlying exception.
    Other dialects can be matched by the constraint name in the message.
    """
    orig = getattr(exc, "orig", None)
    if orig is not None:
        pgcode = getattr(orig, "pgcode", None)
        if pgcode != "23505":
            return False
        constraint_name = getattr(orig, "constraint_name", None)
        if constraint_name is not None:
            return constraint_name == _FEED_UNIQUE_CONSTRAINT
        return _FEED_UNIQUE_CONSTRAINT in str(exc)
    return _FEED_UNIQUE_CONSTRAINT in str(exc)


def _normalized(value: str) -> str | None:
    """Normalize a stored URL for duplicate comparison, or ``None`` if unusable."""
    try:
        return normalize_url(value)
    except ValueError:
        return None


async def add_feed(
    db: Database,
    telegram_user_id: int,
    raw_url: str,
    *,
    fetch_timeout: int = DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> FeedAddOutcome:
    """Validate, probe, and store a feed for the user.

    Validation happens in three escalating steps, cheapest first:

    1. syntax/scheme/credentials (no network, no database);
    2. duplicate check against the user's existing feeds (database, no network);
    3. a single bounded fetch that must produce a usable RSS/Atom document.

    Step 3 is the only network request and it is not repeated: the worker
    performs its own fetch on the next poll, which is necessary because the feed
    keeps changing. Probing first is what stops an HTML page or a dead URL from
    being stored as a subscription that silently never produces news.
    """
    url = raw_url.strip()

    # Reject non-http(s) schemes and embedded credentials before normalization;
    # the shared helper also turns malformed input (a broken IPv6 literal, for
    # example) into an invalid URL instead of a ValueError escaping the handler.
    try:
        parse_http_url(url)
    except ValueError:
        return FeedAddOutcome(FeedAddStatus.INVALID)

    try:
        normalized = normalize_url(url)
    except ValueError:
        return FeedAddOutcome(FeedAddStatus.INVALID)

    # Duplicate check before probing: re-adding a feed must not cost a request.
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)

        for feed in await list_feeds(session, user.id):
            if _normalized(feed.url) == normalized:
                return FeedAddOutcome(FeedAddStatus.DUPLICATE)

    probe = await probe_feed_async(url, fetch_timeout)
    if probe.status is FeedProbeStatus.INVALID:
        return FeedAddOutcome(FeedAddStatus.INVALID)
    if probe.status is FeedProbeStatus.BLOCKED:
        return FeedAddOutcome(FeedAddStatus.BLOCKED)
    if probe.status is FeedProbeStatus.UNREACHABLE:
        return FeedAddOutcome(FeedAddStatus.UNREACHABLE)
    if probe.status is FeedProbeStatus.NOT_A_FEED:
        return FeedAddOutcome(FeedAddStatus.NOT_A_FEED)

    # A separate session: the probe above must never run inside a transaction
    # that holds a database connection for the whole fetch timeout.
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)

        try:
            created = await create_feed(session, user.id, url, title=probe.title)
        except IntegrityError as exc:
            await session.rollback()
            if _is_duplicate_feed_violation(exc):
                return FeedAddOutcome(FeedAddStatus.DUPLICATE)
            raise

    status = (
        FeedAddStatus.EMPTY
        if probe.status is FeedProbeStatus.EMPTY
        else FeedAddStatus.CREATED
    )
    return FeedAddOutcome(status, feed=created)


async def list_user_feeds(db: Database, telegram_user_id: int) -> list[UserFeed]:
    """Return the user's feeds, creating the user row if it does not exist yet."""
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)
        return list(await list_feeds(session, user.id))


async def remove_feed(db: Database, telegram_user_id: int, feed_id: uuid.UUID) -> bool:
    """Delete one of the user's feeds; returns ``False`` if it is not theirs."""
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)
        return await delete_feed(session, user.id, feed_id)


async def remove_feed_by_url(db: Database, telegram_user_id: int, raw_url: str) -> bool:
    """Delete one of the user's feeds by its URL.

    The comparison uses the same normalization as ingestion and duplicate
    detection, so a tracking parameter or a trailing slash does not make the
    user's own feed invisible. Every lookup is scoped to the caller's user id;
    a URL owned by another user is simply not found.
    """
    normalized = _normalized(raw_url)
    if normalized is None:
        return False

    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            return False
        for feed in await list_feeds(session, user.id):
            if _normalized(feed.url) == normalized:
                return await delete_feed(session, user.id, feed.id)
        return False


async def latest_news(
    db: Database, telegram_user_id: int, *, limit: int
) -> list[tuple[NewsItem, UserFeed]]:
    """Return the user's most recent stored articles, newest first."""
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            return []
        return list(await list_news_items_with_feed(session, user.id, limit=limit))


async def delivery_preference(db: Database, telegram_user_id: int) -> bool:
    """Return whether automatic delivery is enabled for the user."""
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)
        return user.auto_delivery_enabled


async def set_delivery_preference(
    db: Database, telegram_user_id: int, enabled: bool
) -> bool:
    """Store the user's automatic-delivery preference and return the new value."""
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)
        await update_user_settings(session, user.id, auto_delivery_enabled=enabled)
    return enabled


async def toggle_delivery_preference(db: Database, telegram_user_id: int) -> bool:
    """Flip the user's automatic-delivery switch and return the new value.

    The flip is a single atomic ``UPDATE`` in the repository, so two concurrent
    ``/delivery`` invocations cannot both read the old value and write the same
    new one. The user row is created first so a brand-new user starts from the
    documented default (ON) and the first toggle turns it OFF.
    """
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)
        toggled = await toggle_auto_delivery(session, user.id)
        if toggled is None:
            # The row existed when it was read, so this can only mean it was
            # deleted concurrently; report the safe default rather than crash.
            return False
        return toggled


async def delivery_interval(db: Database, telegram_user_id: int) -> int:
    """Return the user's minimum spacing between automatic deliveries."""
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)
        return user.delivery_interval_seconds


async def set_delivery_interval(
    db: Database, telegram_user_id: int, seconds: int
) -> int:
    """Store the user's delivery interval and return it.

    Only values from :data:`ai_rss_news_assistant.bot.schedule.DELIVERY_INTERVALS`
    reach this function, because the handler parses the argument first; the
    repository still rejects a negative value defensively.
    """
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)
        await update_user_settings(session, user.id, delivery_interval_seconds=seconds)
    return seconds


async def begin_agent_turn(
    db: Database,
    user_id: uuid.UUID,
    *,
    now: datetime | None = None,
    lease_seconds: int = AGENT_LEASE_SECONDS,
) -> bool:
    """Mark this user as answering a question for a bounded period.

    The worker reads the same column before delivering, so an automatic summary
    cannot interleave with an in-progress answer. A failure to write the lease is
    logged by the caller and must not stop the answer itself.
    """
    moment = now or datetime.now(UTC)
    async with db.session() as session:
        return await acquire_agent_lease(
            session, user_id, moment + timedelta(seconds=lease_seconds)
        )


async def end_agent_turn(db: Database, user_id: uuid.UUID) -> bool:
    """Release the per-user agent lease after an answer finishes or fails."""
    async with db.session() as session:
        return await release_agent_lease(session, user_id)


async def mark_recipient_available(db: Database, telegram_user_id: int) -> bool:
    """Clear a persisted unavailable status after the user contacts the bot."""
    async with db.session() as session:
        return await repository.mark_recipient_available(session, telegram_user_id)


async def answer_question(
    db: Database, answerer: Answerer, telegram_user_id: int, question: str
) -> AgentAnswer:
    """Answer a question from this user's stored news only.

    The internal user id is resolved (and created if needed) before the answerer
    is called, so the agent receives an owner id it can scope retrieval with
    instead of a Telegram identity. A failure of the answerer is reported as a
    fixed, non-technical message.

    A bounded lease is held for the whole call and released in ``finally``, so
    the worker defers automatic delivery for this user while the answer is being
    produced. If the process dies, the lease expires on its own.
    """
    user_id = await ensure_user_id(db, telegram_user_id)

    lease_held = False
    try:
        lease_held = await begin_agent_turn(db, user_id)
    except Exception:
        # Coordination is best-effort: a database hiccup must not make /ask
        # fail, and the worker's own checks still protect the user.
        logger.exception("Failed to acquire the agent processing lease")

    try:
        return await answerer.answer(user_id, question)
    except Exception:  # a handler must never crash polling
        logger.exception("Agent failed to answer a question")
        return AgentAnswer(text=AGENT_ERROR)
    finally:
        if lease_held:
            try:
                await end_agent_turn(db, user_id)
            except Exception:
                logger.exception("Failed to release the agent processing lease")
