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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_rss_news_assistant.db.models import User, UserFeed


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
