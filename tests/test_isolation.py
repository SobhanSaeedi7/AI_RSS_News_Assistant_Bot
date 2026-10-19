"""Integration tests for user-data isolation and database constraints.

These tests run against an in-memory SQLite async engine. They verify that:

* Every record is owned by a user (``user_id`` foreign key).
* Composite foreign keys prevent cross-user references (a message cannot
  reference another user's conversation; a citation cannot reference another
  user's news).
* Cascade deletes remove owned records when a user or feed is deleted.
* Defaults (auto_delivery_enabled) are applied correctly.

No real PostgreSQL, Telegram, LLM, or network is used.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ai_rss_news_assistant.db.base import Base
from ai_rss_news_assistant.db.models import (
    ChatMessage,
    Conversation,
    MessageCitation,
    NewsDelivery,
    NewsItem,
    User,
    UserFeed,
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


async def _create_session() -> AsyncSession:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        # SQLite only enforces foreign keys when this pragma is enabled.
        await conn.execute(text("PRAGMA foreign_keys = ON"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    # Attach engine for cleanup
    session._test_engine = engine  # type: ignore[attr-defined]
    return session


async def _close_session(session: AsyncSession) -> None:
    await session.close()
    engine = getattr(session, "_test_engine", None)
    if engine is not None:
        await engine.dispose()


async def _add_user(session: AsyncSession, telegram_user_id: int = 111) -> User:
    user = User(telegram_user_id=telegram_user_id)
    session.add(user)
    await session.flush()
    return user


async def _add_feed(
    session: AsyncSession, user: User, url: str = "https://example.com/feed.xml"
) -> UserFeed:
    feed = UserFeed(user_id=user.id, url=url, title="Example Feed")
    session.add(feed)
    await session.flush()
    return feed


async def _add_news_item(
    session: AsyncSession,
    user: User,
    feed: UserFeed,
    url: str = "https://example.com/article/1",
) -> NewsItem:
    item = NewsItem(
        user_id=user.id,
        feed_id=feed.id,
        title="Example Article",
        url=url,
        fetched_at=datetime.now(UTC),
    )
    session.add(item)
    await session.flush()
    return item


@pytest.mark.anyio
async def test_user_can_own_feed_and_news() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session)
        feed = await _add_feed(session, user)
        item = await _add_news_item(session, user, feed)

        assert user.id is not None
        assert feed.user_id == user.id
        assert item.user_id == user.id
        assert item.feed_id == feed.id
        assert item.url == "https://example.com/article/1"
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_auto_delivery_defaults_enabled() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session)
        assert user.auto_delivery_enabled is True
        assert user.is_active is True
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_cannot_create_news_for_another_users_feed() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=1)
        other = await _add_user(session, telegram_user_id=2)
        feed = await _add_feed(session, owner)

        # A news item with user_id=other but feed_id=owner's feed violates the
        # composite FK (user_id, feed_id) -> user_feeds(user_id, id).
        item = NewsItem(
            user_id=other.id,
            feed_id=feed.id,
            title="Stolen Article",
            url="https://example.com/stolen",
            fetched_at=datetime.now(UTC),
        )
        session.add(item)
        with pytest.raises(Exception):  # noqa: B017 - DB driver error type varies
            await session.flush()
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_cannot_create_message_for_another_users_conversation() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=1)
        other = await _add_user(session, telegram_user_id=2)
        conversation = Conversation(user_id=owner.id, title="Owner Chat")
        session.add(conversation)
        await session.flush()

        message = ChatMessage(
            user_id=other.id,
            conversation_id=conversation.id,
            role="user",
            content="Should not be allowed",
        )
        session.add(message)
        with pytest.raises(Exception):  # noqa: B017 - DB driver error type varies
            await session.flush()
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_cannot_create_citation_for_another_users_news() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=1)
        other = await _add_user(session, telegram_user_id=2)
        feed = await _add_feed(session, owner)
        item = await _add_news_item(session, owner, feed)
        conversation = Conversation(user_id=other.id, title="Other Chat")
        session.add(conversation)
        await session.flush()
        message = ChatMessage(
            user_id=other.id,
            conversation_id=conversation.id,
            role="assistant",
            content="Response",
        )
        session.add(message)
        await session.flush()

        citation = MessageCitation(
            user_id=other.id,
            chat_message_id=message.id,
            news_item_id=item.id,  # item belongs to owner, not other
        )
        session.add(citation)
        with pytest.raises(Exception):  # noqa: B017 - DB driver error type varies
            await session.flush()
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_user_cannot_see_another_users_news() -> None:
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=10)
        user_b = await _add_user(session, telegram_user_id=20)
        feed_a = await _add_feed(session, user_a, url="https://a.example.com/feed.xml")
        feed_b = await _add_feed(session, user_b, url="https://b.example.com/feed.xml")
        await _add_news_item(
            session, user_a, feed_a, url="https://a.example.com/article/1"
        )
        await _add_news_item(
            session, user_b, feed_b, url="https://b.example.com/article/1"
        )

        result = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user_a.id)
        )
        user_a_items = result.scalars().all()
        assert len(user_a_items) == 1
        assert user_a_items[0].user_id == user_a.id

        result = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user_b.id)
        )
        user_b_items = result.scalars().all()
        assert len(user_b_items) == 1
        assert user_b_items[0].user_id == user_b.id
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_deleting_user_cascades_to_feeds_and_news() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=100)
        feed = await _add_feed(session, user)
        await _add_news_item(session, user, feed)

        await session.delete(user)
        await session.flush()

        result = await session.execute(
            select(UserFeed).where(UserFeed.user_id == user.id)
        )
        assert result.scalars().all() == []

        result = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        assert result.scalars().all() == []
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_deleting_feed_cascades_to_news_and_deliveries() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=200)
        feed = await _add_feed(session, user)
        item = await _add_news_item(session, user, feed)
        delivery = NewsDelivery(
            user_id=user.id,
            news_item_id=item.id,
        )
        session.add(delivery)
        await session.flush()

        await session.delete(feed)
        await session.flush()

        result = await session.execute(
            select(NewsItem).where(NewsItem.feed_id == feed.id)
        )
        assert result.scalars().all() == []

        result = await session.execute(
            select(NewsDelivery).where(NewsDelivery.news_item_id == item.id)
        )
        assert result.scalars().all() == []
    finally:
        await _close_session(session)
