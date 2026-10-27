"""RSS feed ingestion worker.

Runs a polling loop that discovers users with active feeds and ingests
news from each feed using the existing RSS ingestion pipeline.

All user-owned records are processed in dedicated sessions, so a failure
for one user cannot poison the session used for another user.
"""

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from ai_rss_news_assistant.db.html_text_extractor import html_to_text
from ai_rss_news_assistant.db.models import NewsItem, User, UserFeed
from ai_rss_news_assistant.db.rss_ingestion import (
    _fetch_feed_content,
    _safe_url_for_log,
    ingest_feeds_for_user,
)
from ai_rss_news_assistant.worker.delivery import DeliveryRunner

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 30


class NewsWorker:
    """Polls all users' active RSS feeds and persists new articles.

    Runs as a single process. Uses an asyncio lock to prevent overlapping
    polling runs within this process. Does not coordinate across containers.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        poll_interval_seconds: int = 300,
        fetch_full_text: bool = True,
        fetch_timeout: int = 30,
        delivery_service: DeliveryRunner | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be a positive integer")
        if fetch_timeout <= 0:
            raise ValueError("fetch_timeout must be a positive integer")

        self.engine = engine
        self.session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        self.poll_interval_seconds = poll_interval_seconds
        self.fetch_full_text = fetch_full_text
        self.fetch_timeout = fetch_timeout
        self.delivery_service = delivery_service
        self._running = False
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        """Main loop: poll all users' feeds, then sleep until stopped or interval elapses."""
        self._running = True
        self._stop_event.clear()
        while self._running and not self._stop_event.is_set():
            async with self._lock:
                await self._poll_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval_seconds
                )
            except TimeoutError:
                pass

    async def stop(self) -> None:
        """Stop the worker loop, waking any in-progress sleep promptly."""
        self._running = False
        self._stop_event.set()

    async def _poll_once(self) -> None:
        """Poll all users' active feeds once.

        Each user is processed in its own database session, so a failure
        for one user cannot poison the session for another.
        """
        async with self.session_factory() as session:
            users = await self._get_users_with_active_feeds(session)

        for user in users:
            try:
                # begin() commits the user's ingested news when the block exits
                # successfully, and rolls back everything for that user if any
                # step raises. Repository functions only flush; without this
                # commit the session close discarded every poll's results.
                async with self.session_factory.begin() as user_session:
                    await ingest_feeds_for_user(user_session, user.id)
                    if self.fetch_full_text:
                        await self._fetch_missing_full_text(user_session, user.id)
            except Exception:
                logger.exception("Worker error for user %s", user.id)
                continue

            # Delivery runs in its own sessions after ingestion commits, so a
            # Telegram or LLM failure can never roll back stored news.
            if self.delivery_service is not None:
                try:
                    await self.delivery_service.deliver_for_user(user.id)
                except Exception:
                    logger.exception("Delivery error for user %s", user.id)

    async def _get_users_with_active_feeds(self, session: AsyncSession) -> list[User]:
        """Return users who have at least one active feed."""
        result = await session.execute(
            select(User)
            .join(UserFeed, UserFeed.user_id == User.id)
            .where(UserFeed.active == True)
            .distinct()
        )
        return list(result.scalars().all())

    async def _fetch_missing_full_text(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        """Fetch full article text for news items that lack content but have a URL."""
        result = await session.execute(
            select(NewsItem).where(
                NewsItem.user_id == user_id,
                NewsItem.content.is_(None),
                NewsItem.url.is_not(None),
            )
        )
        items = result.scalars().all()

        for item in items:
            try:
                text = await self._extract_text_from_url(item.url)
                if text:
                    item.content = text
                    await session.flush()
            except Exception:
                logger.exception("Failed to fetch full text for news %s", item.id)
                continue

    async def _extract_text_from_url(self, url: str) -> str | None:
        """Fetch article HTML and extract plain text.

        Reuses the existing RSS fetch infrastructure (timeout, size limit,
        SSRF protection) and extracts plain text using the standard library
        HTML parser.
        """
        try:
            content = await asyncio.to_thread(
                _fetch_feed_content, url, self.fetch_timeout
            )
        except Exception:  # noqa: BLE001 - catch-all for transient fetch failures
            logger.debug("Failed to fetch full text from %s", _safe_url_for_log(url))
            return None

        return html_to_text(content.decode("utf-8", errors="replace"))
