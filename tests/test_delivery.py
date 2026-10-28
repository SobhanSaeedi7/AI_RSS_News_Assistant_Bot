"""Automatic delivery tests.

Uses an in-memory SQLite database plus fake Telegram and LLM collaborators.
No real token, network request, or live database is used.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from telegram.error import BadRequest, Forbidden, RetryAfter

from ai_rss_news_assistant.db.base import Base
from ai_rss_news_assistant.db.models import NewsDelivery, NewsItem, User, UserFeed
from ai_rss_news_assistant.db.repository import mark_recipient_available
from ai_rss_news_assistant.worker.delivery import DeliveryService, _source_name
from ai_rss_news_assistant.worker.service import NewsWorker
from ai_rss_news_assistant.worker.summarization import SummarizationError


class FakeSummarizer:
    """Records every summarization call; no LLM request is made."""

    def __init__(self, failing_titles: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.failing_titles = failing_titles or set()

    async def summarize(self, *, title: str, content: str | None) -> str:
        self.calls.append(title)
        if title in self.failing_titles:
            raise SummarizationError("provider unavailable")
        return f"summary of {title}"


class FakeBot:
    """Records sent messages; can simulate blocked chats and bad requests."""

    def __init__(
        self,
        blocked_chats: set[int] | None = None,
        bad_request_chats: set[int] | None = None,
        rate_limited_chats: set[int] | None = None,
    ) -> None:
        self.messages: list[dict] = []
        self.attempts: list[int] = []
        self.blocked_chats = blocked_chats or set()
        self.bad_request_chats = bad_request_chats or set()
        self.rate_limited_chats = rate_limited_chats or set()

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.attempts.append(chat_id)
        if chat_id in self.rate_limited_chats:
            raise RetryAfter(retry_after=30)
        if chat_id in self.blocked_chats:
            raise Forbidden("bot was blocked by the user")
        if chat_id in self.bad_request_chats:
            raise BadRequest("message is too long")
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})


async def _make_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    telegram_user_id: int,
    auto_delivery_enabled: bool = True,
    is_active: bool = True,
    titles: tuple[str, ...] = (),
    delivery_interval_seconds: int = 0,
    last_delivered_at: datetime | None = None,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Create a user with one feed and the supplied news titles."""
    async with factory.begin() as session:
        user = User(
            telegram_user_id=telegram_user_id,
            auto_delivery_enabled=auto_delivery_enabled,
            is_active=is_active,
            delivery_interval_seconds=delivery_interval_seconds,
            last_delivered_at=last_delivered_at,
        )
        session.add(user)
        await session.flush()

        feed = UserFeed(
            user_id=user.id,
            url=f"https://example.com/{telegram_user_id}/feed.xml",
            title="Example Feed",
        )
        session.add(feed)
        await session.flush()

        item_ids: list[uuid.UUID] = []
        for index, title in enumerate(titles):
            item = NewsItem(
                user_id=user.id,
                feed_id=feed.id,
                title=title,
                url=f"https://example.com/{telegram_user_id}/{index}",
                normalized_url=f"https://example.com/{telegram_user_id}/{index}",
                content=f"Body of {title}",
                fetched_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
            )
            session.add(item)
            await session.flush()
            item_ids.append(item.id)

        return user.id, item_ids


async def _delivery_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[NewsDelivery]:
    async with factory() as session:
        result = await session.execute(select(NewsDelivery))
        return list(result.scalars().all())


def test_source_name_falls_back_for_malformed_feed_url() -> None:
    feed = UserFeed(user_id=uuid.uuid4(), url="http://[::1", title=None)

    assert _source_name(feed) == "http://[::1"


@pytest.mark.anyio
async def test_delivery_sends_summaries_and_records_deliveries() -> None:
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory, telegram_user_id=1001, titles=("Item 0", "Item 1")
        )
        bot = FakeBot()
        summarizer = FakeSummarizer()
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=summarizer
        )

        delivered = await service.deliver_for_user(user_id)

        assert delivered == 2
        assert summarizer.calls == ["Item 0", "Item 1"]
        assert len(bot.messages) == 2
        assert bot.messages[0]["chat_id"] == 1001
        assert bot.messages[0]["parse_mode"] == "HTML"
        assert "Item 0" in bot.messages[0]["text"]
        assert "summary of Item 0" in bot.messages[0]["text"]
        assert "Example Feed" in bot.messages[0]["text"]
        assert len(await _delivery_rows(factory)) == 2
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_delivery_off_skips_llm_and_send() -> None:
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory,
            telegram_user_id=1002,
            auto_delivery_enabled=False,
            titles=("Hidden 0", "Hidden 1"),
        )
        bot = FakeBot()
        summarizer = FakeSummarizer()
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=summarizer
        )

        delivered = await service.deliver_for_user(user_id)

        assert delivered == 0
        assert summarizer.calls == []
        assert bot.messages == []
        assert await _delivery_rows(factory) == []
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_delivery_preferences_are_independent() -> None:
    engine, factory = await _make_engine()
    try:
        on_user_id, _ = await _seed_user(
            factory, telegram_user_id=1003, titles=("On item",)
        )
        off_user_id, _ = await _seed_user(
            factory,
            telegram_user_id=1004,
            auto_delivery_enabled=False,
            titles=("Off item",),
        )
        bot = FakeBot()
        summarizer = FakeSummarizer()
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=summarizer
        )

        assert await service.deliver_for_user(off_user_id) == 0
        assert await service.deliver_for_user(on_user_id) == 1

        assert summarizer.calls == ["On item"]
        assert [message["chat_id"] for message in bot.messages] == [1003]
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_delivery_is_not_repeated_across_polls() -> None:
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory, telegram_user_id=1005, titles=("Only once",)
        )
        bot = FakeBot()
        summarizer = FakeSummarizer()
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=summarizer
        )

        assert await service.deliver_for_user(user_id) == 1
        assert await service.deliver_for_user(user_id) == 0

        assert len(bot.messages) == 1
        assert summarizer.calls == ["Only once"]
        assert len(await _delivery_rows(factory)) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_blocked_user_does_not_block_other_users() -> None:
    engine, factory = await _make_engine()
    try:
        blocked_user_id, _ = await _seed_user(
            factory, telegram_user_id=1006, titles=("Blocked item",)
        )
        healthy_user_id, _ = await _seed_user(
            factory, telegram_user_id=1007, titles=("Healthy item",)
        )
        bot = FakeBot(blocked_chats={1006})
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=FakeSummarizer()
        )

        assert await service.deliver_for_user(blocked_user_id) == 0
        assert await service.deliver_for_user(healthy_user_id) == 1

        assert [message["chat_id"] for message in bot.messages] == [1007]
        assert len(await _delivery_rows(factory)) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_blocked_recipient_is_persisted_and_skipped_until_reactivated() -> None:
    """A Forbidden response pauses that user without losing their news."""
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory, telegram_user_id=1030, titles=("Blocked item",)
        )
        bot = FakeBot(blocked_chats={1030})
        summarizer = FakeSummarizer()
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=summarizer
        )

        assert await service.deliver_for_user(user_id) == 0
        assert bot.attempts == [1030]
        assert await _delivery_rows(factory) == []

        stored = await _load_user(factory, user_id)
        assert stored is not None
        assert stored.delivery_unavailable_at is not None

        # A later poll - even from a restarted service - must not attempt a send
        # or pay for another summary.
        restarted = DeliveryService(
            session_factory=factory, bot=bot, summarizer=summarizer
        )
        assert await restarted.deliver_for_user(user_id) == 0
        assert bot.attempts == [1030]
        assert summarizer.calls == ["Blocked item"]

        # Contacting the bot clears the status; the pending item is delivered.
        async with factory.begin() as session:
            assert await mark_recipient_available(session, 1030) is True
        bot.blocked_chats.clear()

        assert await restarted.deliver_for_user(user_id) == 1
        assert len(bot.messages) == 1
        assert len(await _delivery_rows(factory)) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_active_agent_lease_defers_automatic_delivery() -> None:
    """An in-progress /ask pauses delivery for that user only."""
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory, telegram_user_id=1031, titles=("Deferred item",)
        )
        async with factory.begin() as session:
            user = await session.get(User, user_id)
            assert user is not None
            user.agent_processing_until = datetime.now(UTC) + timedelta(seconds=60)

        bot = FakeBot()
        summarizer = FakeSummarizer()
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=summarizer
        )

        assert await service.deliver_for_user(user_id) == 0
        assert bot.messages == []
        assert summarizer.calls == []
        assert await _delivery_rows(factory) == []
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_expired_agent_lease_does_not_defer_delivery() -> None:
    """A lease left behind by a crashed bot expires instead of blocking forever."""
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory, telegram_user_id=1032, titles=("Resumed item",)
        )
        async with factory.begin() as session:
            user = await session.get(User, user_id)
            assert user is not None
            user.agent_processing_until = datetime.now(UTC) - timedelta(seconds=1)

        service = DeliveryService(
            session_factory=factory, bot=FakeBot(), summarizer=FakeSummarizer()
        )

        assert await service.deliver_for_user(user_id) == 1
        assert len(await _delivery_rows(factory)) == 1
    finally:
        await engine.dispose()


class _LeaseSettingSummarizer(FakeSummarizer):
    """Takes the agent lease while summarizing, like a concurrent /ask."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        user_id: uuid.UUID,
        lease_until: datetime,
    ) -> None:
        super().__init__()
        self._factory = factory
        self._user_id = user_id
        self._lease_until = lease_until

    async def summarize(self, *, title: str, content: str | None) -> str:
        summary = await super().summarize(title=title, content=content)
        async with self._factory.begin() as session:
            user = await session.get(User, self._user_id)
            assert user is not None
            user.agent_processing_until = self._lease_until
        return summary


@pytest.mark.anyio
async def test_lease_started_mid_batch_defers_the_remaining_items() -> None:
    """The lease is re-checked between items, not only when the batch is loaded."""
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory, telegram_user_id=1033, titles=("First", "Second")
        )
        start = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        summarizer = _LeaseSettingSummarizer(
            factory, user_id, start + timedelta(seconds=60)
        )
        bot = FakeBot()
        service = DeliveryService(
            session_factory=factory,
            bot=bot,
            summarizer=summarizer,
            max_items_per_run=2,
        )

        assert await service.deliver_for_user(user_id, now=start) == 1
        assert summarizer.calls == ["First"]
        assert len(bot.messages) == 1
        assert len(await _delivery_rows(factory)) == 1

        # The second item was never summarized or sent, so it stays eligible.
        async with factory.begin() as session:
            user = await session.get(User, user_id)
            assert user is not None
            user.agent_processing_until = None

        assert (
            await service.deliver_for_user(user_id, now=start + timedelta(seconds=1))
            == 1
        )
        assert summarizer.calls == ["First", "Second"]
        assert len(await _delivery_rows(factory)) == 2
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_summarization_failure_skips_item_without_recording() -> None:
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory, telegram_user_id=1008, titles=("Broken", "Healthy")
        )
        bot = FakeBot()
        summarizer = FakeSummarizer(failing_titles={"Broken"})
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=summarizer
        )

        assert await service.deliver_for_user(user_id) == 1
        assert [message["chat_id"] for message in bot.messages] == [1008]
        assert "Healthy" in bot.messages[0]["text"]

        deliveries = await _delivery_rows(factory)
        assert len(deliveries) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_delivery_only_includes_own_news() -> None:
    engine, factory = await _make_engine()
    try:
        owner_id, _ = await _seed_user(
            factory, telegram_user_id=1009, titles=("Owner item",)
        )
        other_id, _ = await _seed_user(
            factory, telegram_user_id=1010, titles=("Other item",)
        )
        bot = FakeBot()
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=FakeSummarizer()
        )

        assert await service.deliver_for_user(other_id) == 1
        assert await service.deliver_for_user(owner_id) == 1

        texts = [message["text"] for message in bot.messages]
        assert any("Other item" in text for text in texts)
        assert any("Owner item" in text for text in texts)
        assert sum("Other item" in text for text in texts) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_inactive_user_is_skipped() -> None:
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory, telegram_user_id=1011, is_active=False, titles=("Item",)
        )
        bot = FakeBot()
        summarizer = FakeSummarizer()
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=summarizer
        )

        assert await service.deliver_for_user(user_id) == 0
        assert bot.messages == []
        assert summarizer.calls == []
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_permanent_telegram_error_is_not_retried_forever() -> None:
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory, telegram_user_id=1012, titles=("Poison", "Healthy")
        )
        bot = FakeBot(bad_request_chats={1012})
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=FakeSummarizer()
        )

        # The rejected item is recorded so it cannot block the queue.
        assert await service.deliver_for_user(user_id) == 0
        assert len(await _delivery_rows(factory)) == 2
        assert await service.deliver_for_user(user_id) == 0
        assert bot.messages == []
    finally:
        await engine.dispose()


class FakeDeliveryRunner:
    """Stand-in for DeliveryService used by worker tests."""

    def __init__(self, failing: set[uuid.UUID] | None = None) -> None:
        self.calls: list[uuid.UUID] = []
        self.failing = failing or set()

    async def deliver_for_user(self, user_id: uuid.UUID) -> int:
        self.calls.append(user_id)
        if user_id in self.failing:
            raise RuntimeError("delivery exploded")
        return 1


@pytest.mark.anyio
async def test_worker_runs_delivery_after_ingestion() -> None:
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(factory, telegram_user_id=1013, titles=())
        runner = FakeDeliveryRunner()
        worker = NewsWorker(
            engine=engine, fetch_full_text=False, delivery_service=runner
        )

        with patch(
            "ai_rss_news_assistant.worker.service.ingest_feeds_for_user",
            new_callable=AsyncMock,
            return_value={},
        ):
            await worker._poll_once()

        assert runner.calls == [user_id]
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_worker_delivery_failure_does_not_stop_other_users() -> None:
    engine, factory = await _make_engine()
    try:
        first_id, _ = await _seed_user(factory, telegram_user_id=1014, titles=())
        second_id, _ = await _seed_user(factory, telegram_user_id=1015, titles=())
        runner = FakeDeliveryRunner(failing={first_id})
        worker = NewsWorker(
            engine=engine, fetch_full_text=False, delivery_service=runner
        )

        with patch(
            "ai_rss_news_assistant.worker.service.ingest_feeds_for_user",
            new_callable=AsyncMock,
            return_value={},
        ):
            await worker._poll_once()  # must not raise

        assert set(runner.calls) == {first_id, second_id}
    finally:
        await engine.dispose()


async def _load_user(factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID):
    async with factory() as session:
        return await session.get(User, user_id)


@pytest.mark.anyio
async def test_scheduled_delivery_waits_for_the_interval() -> None:
    """A user interval throttles delivery and skips the LLM entirely."""
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory,
            telegram_user_id=1020,
            titles=("First", "Second"),
            delivery_interval_seconds=3600,
        )
        bot = FakeBot()
        summarizer = FakeSummarizer()
        service = DeliveryService(
            session_factory=factory,
            bot=bot,
            summarizer=summarizer,
            max_items_per_run=1,
        )
        start = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

        assert await service.deliver_for_user(user_id, now=start) == 1
        assert len(bot.messages) == 1

        # Ten minutes later the hour has not passed: no send, no summary.
        assert (
            await service.deliver_for_user(user_id, now=start + timedelta(minutes=10))
            == 0
        )
        assert len(bot.messages) == 1
        assert len(summarizer.calls) == 1

        # The next poll after the interval delivers the remaining item.
        assert (
            await service.deliver_for_user(
                user_id, now=start + timedelta(hours=1, seconds=1)
            )
            == 1
        )
        assert len(bot.messages) == 2
        assert len(summarizer.calls) == 2
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_delivery_off_overrides_the_schedule() -> None:
    """OFF means no delivery regardless of how short the interval is."""
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory,
            telegram_user_id=1021,
            auto_delivery_enabled=False,
            titles=("Never sent",),
            delivery_interval_seconds=0,
        )
        bot = FakeBot()
        summarizer = FakeSummarizer()
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=summarizer
        )

        assert (
            await service.deliver_for_user(
                user_id, now=datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
            )
            == 0
        )

        assert bot.messages == []
        assert summarizer.calls == []
        assert await _delivery_rows(factory) == []
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_schedules_are_independent_between_users() -> None:
    engine, factory = await _make_engine()
    try:
        start = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        slow_id, _ = await _seed_user(
            factory,
            telegram_user_id=1022,
            titles=("Slow item",),
            delivery_interval_seconds=86400,
            last_delivered_at=start,
        )
        fast_id, _ = await _seed_user(
            factory,
            telegram_user_id=1023,
            titles=("Fast item",),
            delivery_interval_seconds=0,
        )
        bot = FakeBot()
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=FakeSummarizer()
        )
        later = start + timedelta(hours=1)

        assert await service.deliver_for_user(slow_id, now=later) == 0
        assert await service.deliver_for_user(fast_id, now=later) == 1

        assert [message["chat_id"] for message in bot.messages] == [1023]
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_schedule_advances_only_after_a_real_delivery() -> None:
    """An empty poll must not postpone the user's next delivery."""
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory,
            telegram_user_id=1024,
            titles=("Only item",),
            delivery_interval_seconds=3600,
        )
        service = DeliveryService(
            session_factory=factory, bot=FakeBot(), summarizer=FakeSummarizer()
        )
        start = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

        assert await service.deliver_for_user(user_id, now=start) == 1

        # SQLite hands the timestamp back without an offset; it is UTC.
        stored = (await _load_user(factory, user_id)).last_delivered_at
        assert stored is not None
        assert stored.replace(tzinfo=UTC) == start

        # Nothing left to send: no delivery, so the timestamp must not move.
        assert (
            await service.deliver_for_user(user_id, now=start + timedelta(hours=3)) == 0
        )
        stored_after = (await _load_user(factory, user_id)).last_delivered_at
        assert stored_after is not None
        assert stored_after.replace(tzinfo=UTC) == start
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_schedule_is_read_from_the_database_not_memory() -> None:
    """A new service instance (a restart) honours the stored schedule."""
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory,
            telegram_user_id=1025,
            titles=("First", "Second"),
            delivery_interval_seconds=3600,
        )
        start = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        first = DeliveryService(
            session_factory=factory,
            bot=FakeBot(),
            summarizer=FakeSummarizer(),
            max_items_per_run=1,
        )
        assert await first.deliver_for_user(user_id, now=start) == 1

        # Simulate a restart: a brand-new service object with no memory.
        bot = FakeBot()
        restarted = DeliveryService(
            session_factory=factory,
            bot=bot,
            summarizer=FakeSummarizer(),
            max_items_per_run=1,
        )
        assert (
            await restarted.deliver_for_user(user_id, now=start + timedelta(minutes=30))
            == 0
        )
        assert bot.messages == []

        assert (
            await restarted.deliver_for_user(user_id, now=start + timedelta(hours=2))
            == 1
        )
        assert len(bot.messages) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_zero_interval_delivers_on_every_poll() -> None:
    """The default keeps the previous behaviour: eligible on each poll."""
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(factory, telegram_user_id=1026, titles=("A", "B"))
        bot = FakeBot()
        summarizer = FakeSummarizer()
        service = DeliveryService(
            session_factory=factory,
            bot=bot,
            summarizer=summarizer,
            max_items_per_run=1,
        )

        assert await service.deliver_for_user(user_id) == 1
        assert await service.deliver_for_user(user_id) == 1

        assert len(bot.messages) == 2
        assert len(summarizer.calls) == 2
        # The same item is never summarized or sent twice.
        assert await service.deliver_for_user(user_id) == 0
        assert len(summarizer.calls) == 2
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_poll_logs_a_summary_without_feed_urls(caplog) -> None:
    """Ingestion statistics must be visible in the logs, without leaking URLs."""
    engine, factory = await _make_engine()
    try:
        await _seed_user(factory, telegram_user_id=1019, titles=())
        worker = NewsWorker(engine=engine, fetch_full_text=False)
        stats = {
            "feeds_processed": 2,
            "feeds_succeeded": 1,
            "items_created": 3,
            "errors": [
                {
                    "feed_id": "feed-1",
                    "feed_url": "https://secret.example/feed.xml",
                    "error": "unreachable",
                }
            ],
        }

        with (
            patch(
                "ai_rss_news_assistant.worker.service.ingest_feeds_for_user",
                new_callable=AsyncMock,
                return_value=stats,
            ),
            caplog.at_level(
                logging.INFO, logger="ai_rss_news_assistant.worker.service"
            ),
        ):
            await worker._poll_once()

        summary = "\n".join(
            record.message
            for record in caplog.records
            if record.name == "ai_rss_news_assistant.worker.service"
        )

        assert "Poll complete" in summary
        assert "feeds_checked=2" in summary
        assert "feeds_ok=1" in summary
        assert "feeds_failed=1" in summary
        assert "items_added=3" in summary
        # The full feed URL is in the stats, but it must never reach the log.
        assert "secret.example" not in summary
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_rate_limited_user_is_not_recorded_and_others_continue() -> None:
    """A Telegram 429 is temporary: it is not a delivery, and not a block."""
    engine, factory = await _make_engine()
    try:
        limited_id, _ = await _seed_user(
            factory, telegram_user_id=1016, titles=("Limited item",)
        )
        healthy_id, _ = await _seed_user(
            factory, telegram_user_id=1017, titles=("Healthy item",)
        )
        bot = FakeBot(rate_limited_chats={1016})
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=FakeSummarizer()
        )

        assert await service.deliver_for_user(limited_id) == 0
        assert await service.deliver_for_user(healthy_id) == 1

        assert [message["chat_id"] for message in bot.messages] == [1017]
        # The rate-limited item stays pending: only the healthy user's is stored.
        assert len(await _delivery_rows(factory)) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_rate_limited_user_is_skipped_until_the_delay_passes() -> None:
    """The delay Telegram asks for is honoured without a tight retry loop."""
    engine, factory = await _make_engine()
    try:
        user_id, _ = await _seed_user(
            factory, telegram_user_id=1018, titles=("Rate limited item",)
        )
        bot = FakeBot(rate_limited_chats={1018})
        service = DeliveryService(
            session_factory=factory, bot=bot, summarizer=FakeSummarizer()
        )

        assert await service.deliver_for_user(user_id) == 0
        assert bot.attempts == [1018]

        # Still inside the Telegram-provided delay: no second attempt at all.
        assert await service.deliver_for_user(user_id) == 0
        assert bot.attempts == [1018]

        # Once the delay has passed the item is delivered and recorded.
        bot.rate_limited_chats.clear()
        service._rate_limited_until[user_id] = datetime.now(UTC) - timedelta(seconds=1)

        assert await service.deliver_for_user(user_id) == 1
        assert len(bot.messages) == 1
        assert len(await _delivery_rows(factory)) == 1
    finally:
        await engine.dispose()
