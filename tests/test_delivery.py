"""Automatic delivery tests.

Uses an in-memory SQLite database plus fake Telegram and LLM collaborators.
No real token, network request, or live database is used.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from telegram.error import BadRequest, Forbidden

from ai_rss_news_assistant.db.base import Base
from ai_rss_news_assistant.db.models import NewsDelivery, NewsItem, User, UserFeed
from ai_rss_news_assistant.db.rss_worker import NewsWorker
from ai_rss_news_assistant.delivery import DeliveryService, _source_name
from ai_rss_news_assistant.summarization import SummarizationError


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
    ) -> None:
        self.messages: list[dict] = []
        self.blocked_chats = blocked_chats or set()
        self.bad_request_chats = bad_request_chats or set()

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
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
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Create a user with one feed and the supplied news titles."""
    async with factory.begin() as session:
        user = User(
            telegram_user_id=telegram_user_id,
            auto_delivery_enabled=auto_delivery_enabled,
            is_active=is_active,
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
            "ai_rss_news_assistant.db.rss_worker.ingest_feeds_for_user",
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
            "ai_rss_news_assistant.db.rss_worker.ingest_feeds_for_user",
            new_callable=AsyncMock,
            return_value={},
        ):
            await worker._poll_once()  # must not raise

        assert set(runner.calls) == {first_id, second_id}
    finally:
        await engine.dispose()
