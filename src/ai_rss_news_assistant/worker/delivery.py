"""Automatic Telegram delivery of newly ingested news.

The delivery loop is owned by the worker and runs after ingestion for each
user. Every user, news item, LLM request, and Telegram send is handled
independently so one failure never stops the remaining work.

Delivery state lives in ``news_deliveries``. A unique constraint on
``(user_id, news_item_id)`` prevents duplicate records when a poll is retried.
"""

from __future__ import annotations

import html
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from ai_rss_news_assistant.db.models import UserFeed
from ai_rss_news_assistant.db.repository import (
    create_delivery,
    get_user_by_id,
    list_undelivered_news_items_with_feed,
    mark_delivery_unavailable,
    record_delivery_run,
)
from ai_rss_news_assistant.worker.summarization import SummarizationError, Summarizer

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4000
MAX_TITLE_LENGTH = 300
MAX_SUMMARY_LENGTH = 1200
MAX_SOURCE_LENGTH = 100
MAX_URL_LENGTH = 1000
DEFAULT_MAX_ITEMS_PER_RUN = 5

# A Telegram 429 carries its own retry delay. Cap what we honour so a hostile or
# absurd value cannot park a user for days; the cap is far above any delay
# Telegram actually sends.
MAX_RATE_LIMIT_DELAY_SECONDS = 3600

Outcome = Literal["delivered", "skipped", "blocked", "rate_limited"]


class TelegramSender(Protocol):
    """Minimal interface implemented by :class:`telegram.Bot`."""

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any: ...


class DeliveryRunner(Protocol):
    """Interface used by the worker to deliver pending news for one user."""

    async def deliver_for_user(self, user_id: uuid.UUID) -> int: ...


@dataclass(frozen=True)
class PendingDelivery:
    """Plain copy of the data needed to deliver one news item."""

    news_item_id: uuid.UUID
    title: str
    url: str
    text: str | None
    published_at: datetime | None
    source: str


def _truncate(value: str, limit: int) -> str:
    """Trim untrusted text before escaping so entities are never cut in half."""
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)].rstrip() + "…"


def _source_name(feed: UserFeed) -> str:
    """Prefer the feed title, falling back to the feed URL's hostname."""
    if feed.title:
        return feed.title
    try:
        return urlsplit(feed.url).hostname or feed.url
    except ValueError:
        return feed.url


def _fit_summary(summary: str, budget: int) -> str:
    """Escape and trim a summary so the escaped text fits ``budget``."""
    limit = min(len(summary), MAX_SUMMARY_LENGTH, budget)
    while limit > 0:
        candidate = html.escape(_truncate(summary, limit), quote=False)
        if len(candidate) <= budget:
            return candidate
        limit = int(limit * 0.75)
    return ""


def schedule_is_due(
    interval_seconds: int, last_delivered_at: datetime | None, now: datetime
) -> bool:
    """Return True when a user's next automatic delivery is eligible.

    ``0`` means "no throttle" (deliver on the next poll). A user who has never
    received an automatic delivery is always due, so setting a weekly cadence
    does not delay the very first message by a week.

    The interval is a minimum spacing, not a timer: the worker only evaluates
    it when it polls, so the real delay is the interval rounded up to the next
    poll.
    """
    if interval_seconds <= 0:
        return True
    if last_delivered_at is None:
        return True

    # PostgreSQL returns an aware timestamp; SQLite (used by the tests) drops
    # the offset. Treat a naive value as UTC rather than raising on comparison.
    if last_delivered_at.tzinfo is None:
        last_delivered_at = last_delivered_at.replace(tzinfo=UTC)

    return last_delivered_at + timedelta(seconds=interval_seconds) <= now


def _as_utc(value: datetime) -> datetime:
    """Treat a naive timestamp (SQLite in tests) as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _agent_answer_in_progress(processing_until: datetime | None, now: datetime) -> bool:
    """Return True while a non-expired per-user agent lease is held."""
    if processing_until is None:
        return False
    return _as_utc(processing_until) > now


def _retry_after_seconds(exc: RetryAfter) -> int:
    """Return the bounded retry delay Telegram asked for, in seconds."""
    raw = getattr(exc, "retry_after", 0)
    if isinstance(raw, timedelta):
        seconds = int(raw.total_seconds())
    else:
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            seconds = 0
    return max(0, min(seconds, MAX_RATE_LIMIT_DELAY_SECONDS))


def _format_message(item: PendingDelivery, summary: str) -> str:
    """Format one article as an HTML-mode Telegram message."""
    title = html.escape(
        _truncate(item.title.strip() or "(بدون عنوان)", MAX_TITLE_LENGTH),
        quote=False,
    )
    source = html.escape(_truncate(item.source, MAX_SOURCE_LENGTH), quote=False)
    url = html.escape(_truncate(item.url, MAX_URL_LENGTH), quote=False)

    lines = [f"<b>{title}</b>", f"منبع: {source}"]
    if item.published_at is not None:
        lines.append(f"تاریخ انتشار: {item.published_at:%Y-%m-%d}")
    lines.append(f"لینک: {url}")
    base = "\n".join(lines)

    clean_summary = " ".join(summary.split())
    budget = MAX_MESSAGE_LENGTH - len(base) - 2
    if not clean_summary or budget <= 0:
        return base[:MAX_MESSAGE_LENGTH]

    return f"{base}\n\n{_fit_summary(clean_summary, budget)}"


class DeliveryService:
    """Summarizes and sends pending news for users with delivery enabled."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        bot: TelegramSender,
        summarizer: Summarizer,
        max_items_per_run: int = DEFAULT_MAX_ITEMS_PER_RUN,
    ) -> None:
        if max_items_per_run <= 0:
            raise ValueError("max_items_per_run must be a positive integer")
        self._session_factory = session_factory
        self._bot = bot
        self._summarizer = summarizer
        self._max_items_per_run = max_items_per_run
        # A Telegram 429 tells us when to try again. Holding that per user in
        # memory honours the delay without blocking the whole worker or the
        # other recipients. It is intentionally not persisted: this is a
        # single-process worker, and losing the value on restart costs at most
        # one early retry.
        self._rate_limited_until: dict[uuid.UUID, datetime] = {}

    async def deliver_for_user(
        self, user_id: uuid.UUID, *, now: datetime | None = None
    ) -> int:
        """Deliver pending news to one user and return how many were sent.

        ``now`` is injectable so the schedule can be tested without waiting;
        production callers leave it unset.
        """
        moment = now or datetime.now(UTC)
        retry_at = self._rate_limited_until.get(user_id)
        if retry_at is not None:
            if retry_at > moment:
                logger.info(
                    "Skipping delivery for user %s: rate limited until %s",
                    user_id,
                    retry_at.isoformat(timespec="seconds"),
                )
                return 0
            self._rate_limited_until.pop(user_id, None)

        pending = await self._load_pending(user_id, moment)
        if pending is None:
            return 0

        telegram_user_id, items = pending
        delivered = 0
        for item in items:
            # Re-check the lease immediately before doing paid work and sending:
            # the bot may have started an /ask after this batch was loaded.
            # Deferred items are left undelivered, so they stay eligible.
            if await self._agent_is_busy(user_id, moment):
                logger.info(
                    "Deferring delivery for user %s: an agent answer is in progress",
                    user_id,
                )
                break

            outcome = await self._deliver_one(user_id, telegram_user_id, item)
            if outcome == "delivered":
                delivered += 1
            elif outcome == "blocked":
                # The user blocked the bot: further sends would fail too.
                break
            elif outcome == "rate_limited":
                # Telegram asked us to slow down; the remaining items for this
                # user wait for the next poll rather than hammering the API.
                break

        if delivered:
            # Only a run that actually sent something advances the schedule, so
            # an empty poll cannot postpone the next real delivery.
            await self._record_run(user_id, moment)

        return delivered

    async def _load_pending(
        self, user_id: uuid.UUID, now: datetime
    ) -> tuple[int, list[PendingDelivery]] | None:
        """Load the user's preferences and undelivered items in one session.

        Returns ``None`` when the user is missing, inactive, has automatic
        delivery disabled, or is not yet due under their own schedule. None of
        those paths lists items or calls the summarizer, so no paid LLM request
        is made for a delivery that cannot happen.
        """
        async with self._session_factory() as session:
            user = await get_user_by_id(session, user_id)
            if user is None or not user.is_active or not user.auto_delivery_enabled:
                return None

            if user.delivery_unavailable_at is not None:
                # Telegram already told us this recipient cannot receive
                # messages. Do not summarize or send again until the user
                # contacts the bot, which clears the status.
                logger.debug(
                    "Skipping delivery for user %s: recipient is unavailable",
                    user_id,
                )
                return None

            if _agent_answer_in_progress(user.agent_processing_until, now):
                logger.info(
                    "Deferring delivery for user %s: an agent answer is in progress",
                    user_id,
                )
                return None

            if not schedule_is_due(
                user.delivery_interval_seconds, user.last_delivered_at, now
            ):
                logger.debug(
                    "Skipping delivery for user %s: next run is not due yet",
                    user_id,
                )
                return None

            rows = await list_undelivered_news_items_with_feed(
                session, user_id, limit=self._max_items_per_run
            )
            items = [
                PendingDelivery(
                    news_item_id=item.id,
                    title=item.title,
                    url=item.url,
                    text=item.content or item.summary,
                    published_at=item.published_at,
                    source=_source_name(feed),
                )
                for item, feed in rows
            ]
            return user.telegram_user_id, items

    async def _record_run(self, user_id: uuid.UUID, delivered_at: datetime) -> None:
        """Persist when this user last received an automatic delivery.

        A failure here is logged, not raised: the messages are already sent and
        the delivery records are already stored, so the only consequence is
        that the schedule is re-evaluated from the previous timestamp.
        """
        async with self._session_factory() as session:
            try:
                await record_delivery_run(session, user_id, delivered_at)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(
                    "Failed to record the delivery run for user %s", user_id
                )

    async def _deliver_one(
        self,
        user_id: uuid.UUID,
        telegram_user_id: int,
        item: PendingDelivery,
    ) -> Outcome:
        """Summarize, send, and record one item without raising."""
        try:
            summary = await self._summarizer.summarize(
                title=item.title, content=item.text
            )
        except SummarizationError as exc:
            logger.warning("Skipping delivery of news %s: %s", item.news_item_id, exc)
            return "skipped"
        except Exception:
            logger.exception(
                "Unexpected summarization failure for news %s", item.news_item_id
            )
            return "skipped"

        message = _format_message(item, summary)

        try:
            await self._bot.send_message(
                chat_id=telegram_user_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except RetryAfter as exc:
            # A 429 is temporary and affects one recipient. Record when this
            # user may be retried and stop their batch; other users continue.
            delay = _retry_after_seconds(exc)
            self._rate_limited_until[user_id] = datetime.now(UTC) + timedelta(
                seconds=delay
            )
            logger.warning(
                "Telegram rate limit for user %s; retrying in %ss",
                telegram_user_id,
                delay,
            )
            return "rate_limited"
        except Forbidden:
            # ``Forbidden`` means the recipient is unavailable (the user
            # blocked the bot, the private chat was deleted, or the bot was
            # removed). Persist the general status so later polls skip this
            # user instead of retrying forever; any new interaction clears it.
            await self._mark_unavailable(user_id)
            logger.warning(
                "Telegram user %s is unavailable; delivery paused for this user",
                telegram_user_id,
            )
            return "blocked"
        except BadRequest as exc:
            # Permanent formatting/content problem: record the delivery so the
            # same item cannot block the queue on every poll.
            logger.warning(
                "Telegram rejected news %s for user %s: %s",
                item.news_item_id,
                telegram_user_id,
                exc.__class__.__name__,
            )
            await self._record_delivery(user_id, item.news_item_id)
            return "skipped"
        except TelegramError as exc:
            logger.warning(
                "Telegram delivery failed for user %s: %s",
                telegram_user_id,
                exc.__class__.__name__,
            )
            return "skipped"
        except Exception:
            logger.exception(
                "Unexpected Telegram failure for user %s", telegram_user_id
            )
            return "skipped"

        await self._record_delivery(user_id, item.news_item_id)
        return "delivered"

    async def _agent_is_busy(self, user_id: uuid.UUID, now: datetime) -> bool:
        """Return True while this user's agent lease is still active."""
        async with self._session_factory() as session:
            user = await get_user_by_id(session, user_id)
            if user is None:
                return True
            return _agent_answer_in_progress(user.agent_processing_until, now)

    async def _mark_unavailable(self, user_id: uuid.UUID) -> None:
        """Persist that Telegram cannot reach this recipient right now."""
        async with self._session_factory() as session:
            try:
                await mark_delivery_unavailable(session, user_id, datetime.now(UTC))
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(
                    "Failed to record the unavailable status for user %s", user_id
                )

    async def _record_delivery(
        self, user_id: uuid.UUID, news_item_id: uuid.UUID
    ) -> None:
        """Persist a delivery record, tolerating a concurrent duplicate."""
        async with self._session_factory() as session:
            try:
                delivery = await create_delivery(session, user_id, news_item_id)
                if delivery is None:
                    logger.warning(
                        "News %s is not owned by user %s; delivery not recorded",
                        news_item_id,
                        user_id,
                    )
                    await session.rollback()
                    return
                await session.commit()
            except IntegrityError:
                # Already recorded by an earlier attempt; the unique
                # constraint on (user_id, news_item_id) did its job.
                await session.rollback()
            except Exception:
                await session.rollback()
                logger.exception("Failed to record delivery of news %s", news_item_id)
