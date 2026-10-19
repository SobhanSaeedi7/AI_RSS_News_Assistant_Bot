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
from ai_rss_news_assistant.db.models import (
    ChatMessage,
    Conversation,
    NewsDelivery,
    NewsItem,
    User,
    UserFeed,
)
from ai_rss_news_assistant.db.repository import (
    create_conversation,
    create_delivery,
    create_feed,
    create_message,
    create_message_citation,
    create_news_item,
    create_user,
    deactivate_user,
    delete_conversation,
    delete_feed,
    get_conversation,
    get_delivery,
    get_feed,
    get_message,
    get_news_item,
    get_user_by_id,
    get_user_by_telegram_id,
    list_conversations,
    list_deliveries,
    list_feeds,
    list_message_citations,
    list_messages,
    list_news_items,
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


async def _add_conversation(
    session: AsyncSession, user: User, title: str | None = None
) -> Conversation:
    conversation = Conversation(user_id=user.id, title=title)
    session.add(conversation)
    await session.flush()
    return conversation


async def _add_message(
    session: AsyncSession,
    user: User,
    conversation: Conversation,
    role: str = "user",
    content: str = "Hello",
) -> ChatMessage:
    message = ChatMessage(
        user_id=user.id,
        conversation_id=conversation.id,
        role=role,
        content=content,
    )
    session.add(message)
    await session.flush()
    return message


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


@pytest.mark.anyio
async def test_create_news_item_for_owned_feed() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=1200)
        feed = await _add_feed(session, user, url="https://example.com/feed.xml")

        item = await create_news_item(
            session,
            user_id=user.id,
            feed_id=feed.id,
            url="https://Example.COM/article/1#section",
            title="Example Article",
        )

        assert item is not None
        assert item.user_id == user.id
        assert item.feed_id == feed.id
        assert item.title == "Example Article"
        assert item.url == "https://Example.COM/article/1#section"
        assert item.normalized_url == "https://example.com/article/1"
        assert item.fetched_at is not None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_news_item_rejects_another_users_feed() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=1300)
        other = await _add_user(session, telegram_user_id=1400)
        feed = await _add_feed(session, owner, url="https://example.com/feed.xml")

        item = await create_news_item(
            session,
            user_id=other.id,
            feed_id=feed.id,
            url="https://example.com/article/1",
            title="Stolen Article",
        )
        assert item is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_news_item_rejects_nonexistent_feed() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=1500)

        item = await create_news_item(
            session,
            user_id=user.id,
            feed_id=uuid.uuid4(),
            url="https://example.com/article/1",
            title="Orphan Article",
        )
        assert item is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_get_news_item_requires_owner() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=1600)
        other = await _add_user(session, telegram_user_id=1700)
        feed = await _add_feed(session, owner, url="https://example.com/feed.xml")
        item = await _add_news_item(
            session, owner, feed, url="https://example.com/article/1"
        )

        fetched = await get_news_item(session, owner.id, item.id)
        assert fetched is not None
        assert fetched.id == item.id

        not_fetched = await get_news_item(session, other.id, item.id)
        assert not_fetched is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_get_news_item_not_found() -> None:
    session = await _create_session()
    try:
        assert await get_news_item(session, uuid.uuid4(), uuid.uuid4()) is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_list_news_items_user_isolation() -> None:
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=1800)
        user_b = await _add_user(session, telegram_user_id=1900)
        feed_a = await _add_feed(session, user_a, url="https://a.example.com/feed.xml")
        feed_b = await _add_feed(session, user_b, url="https://b.example.com/feed.xml")

        await _add_news_item(session, user_a, feed_a, url="https://a.example.com/a1")
        await _add_news_item(session, user_a, feed_a, url="https://a.example.com/a2")
        await _add_news_item(session, user_b, feed_b, url="https://b.example.com/b1")

        items_a = await list_news_items(session, user_a.id)
        items_b = await list_news_items(session, user_b.id)

        assert len(items_a) == 2
        assert all(i.user_id == user_a.id for i in items_a)
        assert len(items_b) == 1
        assert items_b[0].user_id == user_b.id
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_same_normalized_url_different_users_allowed() -> None:
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=2000)
        user_b = await _add_user(session, telegram_user_id=2100)
        feed_a = await _add_feed(session, user_a, url="https://a.example.com/feed.xml")
        feed_b = await _add_feed(session, user_b, url="https://b.example.com/feed.xml")

        await _add_news_item(
            session, user_a, feed_a, url="https://example.com/article/1"
        )
        await _add_news_item(
            session, user_b, feed_b, url="https://example.com/article/1"
        )

        all_items = await session.execute(select(NewsItem))
        items = all_items.scalars().all()
        assert len(items) == 2
        user_ids = {i.user_id for i in items}
        assert user_ids == {user_a.id, user_b.id}
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_same_normalized_url_same_user_rejected() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=2200)
        feed = await _add_feed(session, user)

        await _add_news_item(session, user, feed, url="https://example.com/article/1")

        with pytest.raises(IntegrityError):
            await _add_news_item(
                session, user, feed, url="https://EXAMPLE.COM/article/1"
            )
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_delivery_for_own_news() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=2300)
        feed = await _add_feed(session, user)
        item = await _add_news_item(
            session, user, feed, url="https://example.com/article/1"
        )

        delivery = await create_delivery(session, user.id, item.id)

        assert delivery is not None
        assert delivery.user_id == user.id
        assert delivery.news_item_id == item.id
        assert delivery.delivered_at is not None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_delivery_rejects_another_users_news() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=2400)
        other = await _add_user(session, telegram_user_id=2500)
        feed = await _add_feed(session, owner, url="https://example.com/feed.xml")
        item = await _add_news_item(
            session, owner, feed, url="https://example.com/article/1"
        )

        delivery = await create_delivery(session, other.id, item.id)
        assert delivery is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_delivery_nonexistent_news() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=2600)

        delivery = await create_delivery(session, user.id, uuid.uuid4())
        assert delivery is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_get_delivery_requires_owner() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=2700)
        other = await _add_user(session, telegram_user_id=2800)
        feed = await _add_feed(session, owner, url="https://example.com/feed.xml")
        item = await _add_news_item(
            session, owner, feed, url="https://example.com/article/1"
        )
        delivery = await create_delivery(session, owner.id, item.id)

        fetched = await get_delivery(session, owner.id, delivery.id)
        assert fetched is not None
        assert fetched.id == delivery.id

        not_fetched = await get_delivery(session, other.id, delivery.id)
        assert not_fetched is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_list_deliveries_user_isolation() -> None:
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=2900)
        user_b = await _add_user(session, telegram_user_id=3000)
        feed_a = await _add_feed(session, user_a, url="https://a.example.com/feed.xml")
        feed_b = await _add_feed(session, user_b, url="https://b.example.com/feed.xml")
        item_a = await _add_news_item(
            session, user_a, feed_a, url="https://a.example.com/a1"
        )
        item_b = await _add_news_item(
            session, user_b, feed_b, url="https://b.example.com/b1"
        )

        await create_delivery(session, user_a.id, item_a.id)
        await create_delivery(session, user_b.id, item_b.id)

        deliveries_a = await list_deliveries(session, user_a.id)
        deliveries_b = await list_deliveries(session, user_b.id)

        assert len(deliveries_a) == 1
        assert all(d.user_id == user_a.id for d in deliveries_a)
        assert len(deliveries_b) == 1
        assert deliveries_b[0].user_id == user_b.id
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_duplicate_delivery_rejected() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=3100)
        feed = await _add_feed(session, user)
        item = await _add_news_item(
            session, user, feed, url="https://example.com/article/1"
        )

        await create_delivery(session, user.id, item.id)

        with pytest.raises(IntegrityError):
            await create_delivery(session, user.id, item.id)
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_and_get_conversation() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=3200)
        conversation = await create_conversation(session, user.id, title="My Chat")

        fetched = await get_conversation(session, user.id, conversation.id)
        assert fetched is not None
        assert fetched.id == conversation.id
        assert fetched.user_id == user.id
        assert fetched.title == "My Chat"
        assert fetched.created_at is not None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_get_conversation_requires_owner() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=3300)
        other = await _add_user(session, telegram_user_id=3400)
        conversation = await create_conversation(session, owner.id, title="Owner Chat")

        fetched = await get_conversation(session, owner.id, conversation.id)
        assert fetched is not None
        assert fetched.id == conversation.id

        not_fetched = await get_conversation(session, other.id, conversation.id)
        assert not_fetched is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_get_conversation_not_found() -> None:
    session = await _create_session()
    try:
        assert await get_conversation(session, uuid.uuid4(), uuid.uuid4()) is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_list_conversations_user_isolation() -> None:
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=3500)
        user_b = await _add_user(session, telegram_user_id=3600)
        await _add_conversation(session, user_a, title="Chat A")
        await _add_conversation(session, user_b, title="Chat B")

        conversations_a = await list_conversations(session, user_a.id)
        conversations_b = await list_conversations(session, user_b.id)

        assert len(conversations_a) == 1
        assert all(c.user_id == user_a.id for c in conversations_a)
        assert len(conversations_b) == 1
        assert conversations_b[0].user_id == user_b.id
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_delete_conversation_by_owner() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=3700)
        conversation = await create_conversation(session, owner.id, title="To Delete")

        assert await delete_conversation(session, owner.id, conversation.id) is True
        assert await delete_conversation(session, owner.id, conversation.id) is False

        other = await _add_user(session, telegram_user_id=3800)
        conversation2 = await create_conversation(session, other.id, title="Other")
        assert await delete_conversation(session, owner.id, conversation2.id) is False
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_message_for_owned_conversation() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=3900)
        conversation = await _add_conversation(session, user)

        message = await create_message(
            session,
            user.id,
            conversation.id,
            role="user",
            content="Hello, world!",
        )

        assert message is not None
        assert message.user_id == user.id
        assert message.conversation_id == conversation.id
        assert message.role == "user"
        assert message.content == "Hello, world!"
        assert message.created_at is not None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_message_rejects_another_users_conversation() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=4000)
        other = await _add_user(session, telegram_user_id=4100)
        conversation = await _add_conversation(session, owner)

        message = await create_message(
            session,
            other.id,
            conversation.id,
            role="user",
            content="Should not be allowed",
        )
        assert message is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_get_message_requires_owner() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=4200)
        other = await _add_user(session, telegram_user_id=4300)
        conversation = await _add_conversation(session, owner)
        message = await _add_message(session, owner, conversation)

        fetched = await get_message(session, owner.id, message.id)
        assert fetched is not None
        assert fetched.id == message.id

        not_fetched = await get_message(session, other.id, message.id)
        assert not_fetched is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_list_messages_user_isolation_and_ordering() -> None:
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=4400)
        user_b = await _add_user(session, telegram_user_id=4500)
        conversation_a = await _add_conversation(session, user_a)
        conversation_b = await _add_conversation(session, user_b)

        msg_a1 = await _add_message(
            session, user_a, conversation_a, role="user", content="First"
        )
        msg_a2 = await _add_message(
            session, user_a, conversation_a, role="assistant", content="Second"
        )
        await _add_message(
            session, user_b, conversation_b, role="user", content="Other user"
        )

        messages_a = await list_messages(session, user_a.id, conversation_a.id)

        assert len(messages_a) == 2
        assert messages_a[0].id == msg_a1.id
        assert messages_a[0].content == "First"
        assert messages_a[1].id == msg_a2.id
        assert messages_a[1].content == "Second"
        assert all(m.user_id == user_a.id for m in messages_a)
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_list_messages_empty_for_other_conversation() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=4600)
        conversation_a = await _add_conversation(session, user)
        conversation_b = await _add_conversation(session, user)
        await _add_message(session, user, conversation_a)

        messages_b = await list_messages(session, user.id, conversation_b.id)
        assert messages_b == []
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_message_citation_for_own_message_and_news() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=4700)
        conversation = await _add_conversation(session, user)
        message = await _add_message(session, user, conversation)
        feed = await _add_feed(session, user)
        item = await _add_news_item(
            session, user, feed, url="https://example.com/article/1"
        )

        citation = await create_message_citation(session, user.id, message.id, item.id)

        assert citation is not None
        assert citation.user_id == user.id
        assert citation.chat_message_id == message.id
        assert citation.news_item_id == item.id
        assert citation.created_at is not None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_citation_rejects_another_users_message() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=4800)
        other = await _add_user(session, telegram_user_id=4900)
        conversation = await _add_conversation(session, owner)
        message = await _add_message(session, owner, conversation)
        feed = await _add_feed(session, owner)
        item = await _add_news_item(
            session, owner, feed, url="https://example.com/article/1"
        )

        citation = await create_message_citation(session, other.id, message.id, item.id)
        assert citation is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_citation_rejects_another_users_news() -> None:
    session = await _create_session()
    try:
        owner = await _add_user(session, telegram_user_id=5000)
        other = await _add_user(session, telegram_user_id=5100)
        feed_other = await _add_feed(
            session, other, url="https://other.example.com/feed.xml"
        )
        conversation = await _add_conversation(session, owner)
        message = await _add_message(session, owner, conversation)
        item_other = await _add_news_item(
            session, other, feed_other, url="https://other.example.com/article/1"
        )

        citation = await create_message_citation(
            session, owner.id, message.id, item_other.id
        )
        assert citation is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_create_citation_nonexistent_message() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=5200)
        feed = await _add_feed(session, user)
        item = await _add_news_item(
            session, user, feed, url="https://example.com/article/1"
        )

        citation = await create_message_citation(
            session, user.id, uuid.uuid4(), item.id
        )
        assert citation is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_list_message_citations_user_isolation() -> None:
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=5300)
        user_b = await _add_user(session, telegram_user_id=5400)
        conversation_a = await _add_conversation(session, user_a)
        conversation_b = await _add_conversation(session, user_b)
        message_a = await _add_message(session, user_a, conversation_a)
        message_b = await _add_message(session, user_b, conversation_b)
        feed_a = await _add_feed(session, user_a, url="https://a.example.com/feed.xml")
        feed_b = await _add_feed(session, user_b, url="https://b.example.com/feed.xml")
        item_a = await _add_news_item(
            session, user_a, feed_a, url="https://a.example.com/a1"
        )
        item_b = await _add_news_item(
            session, user_b, feed_b, url="https://b.example.com/b1"
        )

        await create_message_citation(session, user_a.id, message_a.id, item_a.id)
        await create_message_citation(session, user_b.id, message_b.id, item_b.id)

        citations_a = await list_message_citations(session, user_a.id, message_a.id)
        citations_b = await list_message_citations(session, user_b.id, message_b.id)

        assert len(citations_a) == 1
        assert citations_a[0].user_id == user_a.id
        assert citations_a[0].news_item_id == item_a.id
        assert len(citations_b) == 1
        assert citations_b[0].user_id == user_b.id
        assert citations_b[0].news_item_id == item_b.id
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_delete_conversation_cascades_to_messages_and_citations() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=5500)
        conversation = await _add_conversation(session, user)
        message = await _add_message(session, user, conversation)
        feed = await _add_feed(session, user)
        item = await _add_news_item(
            session, user, feed, url="https://example.com/article/1"
        )
        await create_message_citation(session, user.id, message.id, item.id)

        assert await delete_conversation(session, user.id, conversation.id) is True

        messages = await list_messages(session, user.id, conversation.id)
        assert messages == []

        citations = await list_message_citations(session, user.id, message.id)
        assert citations == []
    finally:
        await _close_session(session)
