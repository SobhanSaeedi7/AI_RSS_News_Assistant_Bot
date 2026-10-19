"""Repository-layer tests for users and feeds.

Uses an in-memory SQLite async engine. All ownership checks happen inside the
database query (never fetch-by-ID then check ownership in Python).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ai_rss_news_assistant.db.base import Base
from ai_rss_news_assistant.db.models import NewsDelivery, NewsItem, User, UserFeed
from ai_rss_news_assistant.db.repository import (
    create_feed,
    create_user,
    deactivate_user,
    delete_feed,
    get_feed,
    get_user_by_id,
    get_user_by_telegram_id,
    list_feeds,
    update_feed,
    update_user_settings,
)
from ai_rss_news_assistant.db.url_normalization import normalize_url

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


async def _create_session() -> AsyncSession:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys = ON"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
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
        normalized_url=normalize_url(url),
        fetched_at=datetime.now(UTC),
    )
    session.add(item)
    await session.flush()
    return item


@pytest.mark.anyio
async def test_create_and_get_user_by_telegram_id() -> None:
    session = await _create_session()
    try:
        user = await create_user(session, telegram_user_id=111)
        fetched = await get_user_by_telegram_id(session, 111)
        assert fetched is not None
        assert fetched.id == user.id
        assert fetched.telegram_user_id == 111
        assert fetched.auto_delivery_enabled is True
        assert fetched.is_active is True
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_user_duplicate_telegram_id_raises() -> None:
    session = await _create_session()
    try:
        await create_user(session, telegram_user_id=222)
        with pytest.raises(IntegrityError):
            await create_user(session, telegram_user_id=222)
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_get_user_by_id() -> None:
    session = await _create_session()
    try:
        user = await create_user(session, telegram_user_id=333)
        fetched = await get_user_by_id(session, user.id)
        assert fetched is not None
        assert fetched.id == user.id
        assert fetched.telegram_user_id == 333
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_get_user_by_id_missing() -> None:
    session = await _create_session()
    try:
        assert await get_user_by_id(session, uuid.uuid4()) is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_update_user_settings() -> None:
    session = await _create_session()
    try:
        user = await create_user(session, telegram_user_id=444)
        updated = await update_user_settings(
            session,
            user.id,
            auto_delivery_enabled=False,
            is_active=False,
        )
        assert updated is not None
        assert updated.auto_delivery_enabled is False
        assert updated.is_active is False

        fetched = await get_user_by_id(session, user.id)
        assert fetched.auto_delivery_enabled is False
        assert fetched.is_active is False
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_update_user_settings_missing() -> None:
    session = await _create_session()
    try:
        assert (
            await update_user_settings(
                session, uuid.uuid4(), auto_delivery_enabled=False
            )
            is None
        )
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_deactivate_user() -> None:
    session = await _create_session()
    try:
        user = await create_user(session, telegram_user_id=555)
        assert await deactivate_user(session, user.id) is True
        fetched = await get_user_by_id(session, user.id)
        assert fetched.is_active is False
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_deactivate_user_missing() -> None:
    session = await _create_session()
    try:
        assert await deactivate_user(session, uuid.uuid4()) is False
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_and_list_feeds_user_isolation() -> None:
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=600)
        user_b = await _add_user(session, telegram_user_id=700)

        feed_a1 = await create_feed(
            session, user_a.id, url="https://a.example.com/feed1.xml", title="Feed A1"
        )
        feed_a2 = await create_feed(
            session, user_a.id, url="https://a.example.com/feed2.xml", title="Feed A2"
        )
        feed_b = await create_feed(
            session, user_b.id, url="https://b.example.com/feed.xml", title="Feed B"
        )

        feeds_a = await list_feeds(session, user_a.id)
        feeds_b = await list_feeds(session, user_b.id)

        assert {f.id for f in feeds_a} == {feed_a1.id, feed_a2.id}
        assert all(f.user_id == user_a.id for f in feeds_a)
        assert len(feeds_b) == 1
        assert feeds_b[0].id == feed_b.id
        assert feeds_b[0].user_id == user_b.id
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_get_feed_requires_owner() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=800)
        other = await _add_user(session, telegram_user_id=900)
        feed = await create_feed(session, owner.id, url="https://example.com/feed.xml")

        fetched = await get_feed(session, owner.id, feed.id)
        assert fetched is not None
        assert fetched.id == feed.id
        assert fetched.user_id == owner.id

        not_fetched = await get_feed(session, other.id, feed.id)
        assert not_fetched is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_get_feed_not_found() -> None:
    session = await _create_session()
    try:
        assert await get_feed(session, uuid.uuid4(), uuid.uuid4()) is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_feed_update_and_delete_by_owner() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=1000)
        feed = await create_feed(
            session, owner.id, url="https://example.com/feed.xml", title="Original"
        )

        updated = await update_feed(
            session, owner.id, feed.id, active=False, title="Renamed"
        )
        assert updated is not None
        assert updated.active is False
        assert updated.title == "Renamed"

        fetched = await get_feed(session, owner.id, feed.id)
        assert fetched.active is False
        assert fetched.title == "Renamed"

        other = await _add_user(session, telegram_user_id=1001)
        assert await update_feed(session, other.id, feed.id, active=True) is None

        assert await delete_feed(session, owner.id, feed.id) is True
        assert await delete_feed(session, owner.id, feed.id) is False
        assert await delete_feed(session, other.id, feed.id) is False
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_delete_feed_cascades_to_news_and_delivery() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=1100)
        feed = await _add_feed(session, user)
        item = await _add_news_item(
            session, user, feed, url="https://example.com/article/1"
        )
        delivery = NewsDelivery(user_id=user.id, news_item_id=item.id)
        session.add(delivery)
        await session.flush()

        assert await delete_feed(session, user.id, feed.id) is True

        news = await session.execute(
            select(NewsItem).where(NewsItem.feed_id == feed.id)
        )
        assert news.scalars().all() == []

        deliveries = await session.execute(
            select(NewsDelivery).where(NewsDelivery.news_item_id == item.id)
        )
        assert deliveries.scalars().all() == []
    finally:
        await _close_session(session)
