"""User and feed repository functions.

All functions are ownership-aware: every read, update, or delete of a
user-owned record filters by ``user_id`` in the database query. Records are
never fetched by ID first and checked for ownership afterwards.

Return convention:
* Ownership-scoped lookups return ``None`` when no matching record exists.
* Update/delete operations return ``False`` when they affect no row
  (``update_feed`` returns the updated object or ``None``).
* Functions do not commit or roll back the caller's session; they use
  ``flush()`` only, leaving transaction ownership to the session manager
  (``db/base.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_rss_news_assistant.db.models import NewsDelivery, NewsItem, User, UserFeed
from ai_rss_news_assistant.db.url_normalization import normalize_url


async def get_user_by_telegram_id(
    session: AsyncSession, telegram_user_id: int
) -> User | None:
    result = await session.execute(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    telegram_user_id: int,
    auto_delivery_enabled: bool = True,
    is_active: bool = True,
) -> User:
    user = User(
        telegram_user_id=telegram_user_id,
        auto_delivery_enabled=auto_delivery_enabled,
        is_active=is_active,
    )
    session.add(user)
    await session.flush()
    return user


async def update_user_settings(
    session: AsyncSession,
    user_id: uuid.UUID,
    auto_delivery_enabled: bool | None = None,
    is_active: bool | None = None,
) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    if auto_delivery_enabled is not None:
        user.auto_delivery_enabled = auto_delivery_enabled
    if is_active is not None:
        user.is_active = is_active
    await session.flush()
    return user


async def deactivate_user(session: AsyncSession, user_id: uuid.UUID) -> bool:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        return False
    user.is_active = False
    await session.flush()
    return True


async def create_feed(
    session: AsyncSession,
    user_id: uuid.UUID,
    url: str,
    title: str | None = None,
) -> UserFeed:
    feed = UserFeed(user_id=user_id, url=url, title=title)
    session.add(feed)
    await session.flush()
    return feed


async def get_feed(
    session: AsyncSession, user_id: uuid.UUID, feed_id: uuid.UUID
) -> UserFeed | None:
    result = await session.execute(
        select(UserFeed).where(UserFeed.id == feed_id, UserFeed.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def list_feeds(session: AsyncSession, user_id: uuid.UUID) -> list[UserFeed]:
    result = await session.execute(select(UserFeed).where(UserFeed.user_id == user_id))
    return list(result.scalars().all())


async def update_feed(
    session: AsyncSession,
    user_id: uuid.UUID,
    feed_id: uuid.UUID,
    active: bool | None = None,
    title: str | None = None,
) -> UserFeed | None:
    result = await session.execute(
        select(UserFeed).where(UserFeed.id == feed_id, UserFeed.user_id == user_id)
    )
    feed = result.scalar_one_or_none()
    if feed is None:
        return None
    if active is not None:
        feed.active = active
    if title is not None:
        feed.title = title
    await session.flush()
    return feed


async def delete_feed(
    session: AsyncSession, user_id: uuid.UUID, feed_id: uuid.UUID
) -> bool:
    result = await session.execute(
        select(UserFeed).where(UserFeed.id == feed_id, UserFeed.user_id == user_id)
    )
    feed = result.scalar_one_or_none()
    if feed is None:
        return False
    await session.delete(feed)
    await session.flush()
    return True


async def create_news_item(
    session: AsyncSession,
    user_id: uuid.UUID,
    feed_id: uuid.UUID,
    url: str,
    title: str,
    normalized_url: str | None = None,
    content: str | None = None,
    summary: str | None = None,
    published_at: datetime | None = None,
    fetched_at: datetime | None = None,
) -> NewsItem | None:
    """Create a news item for a feed owned by ``user_id``.

    Returns ``None`` if the feed does not exist or is not owned by ``user_id``.
    ``normalized_url`` is computed from ``url`` via :func:`normalize_url` when
    not supplied explicitly.
    """
    feed = await session.execute(
        select(UserFeed).where(UserFeed.id == feed_id, UserFeed.user_id == user_id)
    )
    if feed.scalar_one_or_none() is None:
        return None

    if normalized_url is None:
        normalized_url = normalize_url(url)

    item = NewsItem(
        user_id=user_id,
        feed_id=feed_id,
        title=title,
        url=url,
        normalized_url=normalized_url,
        content=content,
        summary=summary,
        published_at=published_at,
        fetched_at=fetched_at or datetime.now(UTC),
    )
    session.add(item)
    await session.flush()
    return item


async def get_news_item(
    session: AsyncSession, user_id: uuid.UUID, news_item_id: uuid.UUID
) -> NewsItem | None:
    result = await session.execute(
        select(NewsItem).where(NewsItem.id == news_item_id, NewsItem.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def list_news_items(
    session: AsyncSession,
    user_id: uuid.UUID,
    limit: int | None = None,
    offset: int | None = None,
) -> list[NewsItem]:
    query = (
        select(NewsItem)
        .where(NewsItem.user_id == user_id)
        .order_by(NewsItem.fetched_at.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    if offset is not None:
        query = query.offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def create_delivery(
    session: AsyncSession,
    user_id: uuid.UUID,
    news_item_id: uuid.UUID,
) -> NewsDelivery | None:
    """Create a delivery record for a news item owned by ``user_id``.

    Returns ``None`` if the news item does not exist or is not owned by
    ``user_id``. The unique constraint on ``(user_id, news_item_id)`` also
    prevents duplicate deliveries at the database level.
    """
    news = await session.execute(
        select(NewsItem).where(NewsItem.id == news_item_id, NewsItem.user_id == user_id)
    )
    if news.scalar_one_or_none() is None:
        return None

    delivery = NewsDelivery(
        user_id=user_id,
        news_item_id=news_item_id,
    )
    session.add(delivery)
    await session.flush()
    return delivery


async def get_delivery(
    session: AsyncSession, user_id: uuid.UUID, delivery_id: uuid.UUID
) -> NewsDelivery | None:
    result = await session.execute(
        select(NewsDelivery).where(
            NewsDelivery.id == delivery_id, NewsDelivery.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def list_deliveries(
    session: AsyncSession,
    user_id: uuid.UUID,
    limit: int | None = None,
    offset: int | None = None,
) -> list[NewsDelivery]:
    query = (
        select(NewsDelivery)
        .where(NewsDelivery.user_id == user_id)
        .order_by(NewsDelivery.delivered_at.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    if offset is not None:
        query = query.offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())
