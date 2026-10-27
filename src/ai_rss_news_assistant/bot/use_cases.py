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
from enum import Enum
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from ai_rss_news_assistant.contracts import AGENT_ERROR, AgentAnswer
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.db.models import NewsItem, UserFeed
from ai_rss_news_assistant.db.repository import (
    create_feed,
    create_user,
    delete_feed,
    get_user_by_telegram_id,
    list_feeds,
    list_news_items_with_feed,
    update_user_settings,
)
from ai_rss_news_assistant.db.url_normalization import normalize_url
from ai_rss_news_assistant.urls import parse_http_url

logger = logging.getLogger(__name__)

# Unique constraint on (user_id, url); used to tell a real duplicate apart from
# another integrity failure, which must not be reported as "already added".
_FEED_UNIQUE_CONSTRAINT = "uq_user_feeds_user_id_url"


class Answerer(Protocol):
    """Anything that can answer a question for one internal user id."""

    async def answer(self, user_id: uuid.UUID, question: str) -> AgentAnswer:
        """Return an answer drawn only from that user's stored news."""
        ...


class FeedAddStatus(Enum):
    """Outcome of an /addfeed request."""

    CREATED = "created"
    DUPLICATE = "duplicate"
    INVALID = "invalid"


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
    db: Database, telegram_user_id: int, raw_url: str
) -> FeedAddOutcome:
    """Validate and store a feed for the user, reporting duplicates and rejects.

    Non-http(s) schemes and embedded credentials are rejected before anything is
    stored, because credentials could otherwise be persisted and logged.
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

    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)

        for feed in await list_feeds(session, user.id):
            if _normalized(feed.url) == normalized:
                return FeedAddOutcome(FeedAddStatus.DUPLICATE)

        try:
            created = await create_feed(session, user.id, url)
        except IntegrityError as exc:
            await session.rollback()
            if _is_duplicate_feed_violation(exc):
                return FeedAddOutcome(FeedAddStatus.DUPLICATE)
            raise

    return FeedAddOutcome(FeedAddStatus.CREATED, feed=created)


async def list_user_feeds(db: Database, telegram_user_id: int) -> list[UserFeed]:
    """Return the user's feeds, creating the user row if it does not exist yet."""
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)
        return list(await list_feeds(session, user.id))


async def remove_feed(
    db: Database, telegram_user_id: int, feed_id: uuid.UUID
) -> bool:
    """Delete one of the user's feeds; returns ``False`` if it is not theirs."""
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        if user is None:
            user = await create_user(session, telegram_user_id)
        return await delete_feed(session, user.id, feed_id)


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


async def answer_question(
    db: Database, answerer: Answerer, telegram_user_id: int, question: str
) -> AgentAnswer:
    """Answer a question from this user's stored news only.

    The internal user id is resolved (and created if needed) before the answerer
    is called, so the agent receives an owner id it can scope retrieval with
    instead of a Telegram identity. A failure of the answerer is reported as a
    fixed, non-technical message.
    """
    user_id = await ensure_user_id(db, telegram_user_id)
    try:
        return await answerer.answer(user_id, question)
    except Exception:  # a handler must never crash polling
        logger.exception("Agent failed to answer a question")
        return AgentAnswer(text=AGENT_ERROR)
