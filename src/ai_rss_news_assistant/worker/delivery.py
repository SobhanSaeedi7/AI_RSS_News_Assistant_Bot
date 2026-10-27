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
from datetime import datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram.error import BadRequest, Forbidden, TelegramError

from ai_rss_news_assistant.db.models import UserFeed
from ai_rss_news_assistant.db.repository import (
    create_delivery,
    get_user_by_id,
    list_undelivered_news_items_with_feed,
)
from ai_rss_news_assistant.worker.summarization import SummarizationError, Summarizer

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4000
MAX_TITLE_LENGTH = 300
MAX_SUMMARY_LENGTH = 1200
MAX_SOURCE_LENGTH = 100
MAX_URL_LENGTH = 1000
DEFAULT_MAX_ITEMS_PER_RUN = 5

Outcome = Literal["delivered", "skipped", "blocked"]


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

    async def deliver_for_user(self, user_id: uuid.UUID) -> int:
        """Deliver pending news to one user and return how many were sent."""
        pending = await self._load_pending(user_id)
        if pending is None:
            return 0

        telegram_user_id, items = pending
        delivered = 0
        for item in items:
            outcome = await self._deliver_one(user_id, telegram_user_id, item)
            if outcome == "delivered":
                delivered += 1
            elif outcome == "blocked":
                # The user blocked the bot: further sends would fail too.
                break

        return delivered

    async def _load_pending(
        self, user_id: uuid.UUID
    ) -> tuple[int, list[PendingDelivery]] | None:
        """Load the user's preference and undelivered items in one session.

        Returns ``None`` when the user is missing, inactive, or has automatic
        delivery disabled, in which case no LLM request is made.
        """
        async with self._session_factory() as session:
            user = await get_user_by_id(session, user_id)
            if user is None or not user.is_active or not user.auto_delivery_enabled:
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
        except Forbidden:
            logger.warning(
                "Telegram user %s has blocked the bot; delivery paused for this user",
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
