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

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_rss_news_assistant.db.models import (
    ChatMessage,
    Conversation,
    MessageCitation,
    NewsDelivery,
    NewsItem,
    User,
    UserFeed,
)
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


async def list_news_items_with_feed(
    session: AsyncSession,
    user_id: uuid.UUID,
    limit: int = 5,
) -> list[tuple[NewsItem, UserFeed]]:
    """Return the user's latest news items paired with the feed they came from.

    The join is scoped by ``user_id`` on both sides, so a news item can only
    be paired with a feed owned by the same user.
    """
    result = await session.execute(
        select(NewsItem, UserFeed)
        .join(
            UserFeed,
            (UserFeed.id == NewsItem.feed_id) & (UserFeed.user_id == NewsItem.user_id),
        )
        .where(NewsItem.user_id == user_id)
        .order_by(NewsItem.fetched_at.desc(), NewsItem.url)
        .limit(limit)
    )
    return [(item, feed) for item, feed in result.all()]


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so user text is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_news_items(
    session: AsyncSession,
    user_id: uuid.UUID,
    terms: list[str],
    limit: int = 5,
) -> list[tuple[NewsItem, UserFeed]]:
    """Return the user's news items matching any of ``terms``, best first.

    Matching is a case-insensitive substring search over the title, stored
    summary, stored content, and feed title. Candidates are re-ranked in
    Python by the number of distinct query terms they contain, then by
    recency. This is deterministic keyword matching, not semantic search:
    PostgreSQL's built-in full-text search has no Persian configuration.

    The query is always scoped to ``user_id`` on both the news item and its
    feed, so another user's rows can never be returned.
    """
    if not terms or limit <= 0:
        return []

    conditions = []
    for term in terms:
        pattern = f"%{_escape_like(term)}%"
        conditions.append(
            or_(
                NewsItem.title.ilike(pattern, escape="\\"),
                NewsItem.summary.ilike(pattern, escape="\\"),
                NewsItem.content.ilike(pattern, escape="\\"),
                UserFeed.title.ilike(pattern, escape="\\"),
            )
        )

    result = await session.execute(
        select(NewsItem, UserFeed)
        .join(
            UserFeed,
            (UserFeed.id == NewsItem.feed_id) & (UserFeed.user_id == NewsItem.user_id),
        )
        .where(NewsItem.user_id == user_id, or_(*conditions))
        .order_by(NewsItem.fetched_at.desc(), NewsItem.url)
        .limit(limit * 4)
    )

    ranked: list[tuple[int, NewsItem, UserFeed]] = []
    for item, feed in result.all():
        haystack = " ".join(
            part
            for part in (item.title, item.summary, item.content, feed.title)
            if part
        ).lower()
        score = sum(1 for term in terms if term in haystack)
        ranked.append((score, item, feed))

    ranked.sort(key=lambda row: (row[0], row[1].fetched_at), reverse=True)
    return [(item, feed) for _, item, feed in ranked[:limit]]


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


async def create_conversation(
    session: AsyncSession,
    user_id: uuid.UUID,
    title: str | None = None,
) -> Conversation:
    """Create a conversation owned by ``user_id``."""
    conversation = Conversation(user_id=user_id, title=title)
    session.add(conversation)
    await session.flush()
    return conversation


async def get_conversation(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation | None:
    result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def list_conversations(
    session: AsyncSession,
    user_id: uuid.UUID,
    limit: int | None = None,
    offset: int | None = None,
) -> list[Conversation]:
    query = (
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    if offset is not None:
        query = query.offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def delete_conversation(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> bool:
    """Delete a conversation owned by ``user_id``.

    Returns ``False`` when no matching conversation exists.
    """
    result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        return False
    await session.delete(conversation)
    await session.flush()
    return True


async def create_message(
    session: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
) -> ChatMessage | None:
    """Create a message for a conversation owned by ``user_id``.

    Returns ``None`` if the conversation does not exist or is not owned by
    ``user_id``.
    """
    conversation = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conversation.scalar_one_or_none() is None:
        return None

    message = ChatMessage(
        user_id=user_id,
        conversation_id=conversation_id,
        role=role,
        content=content,
    )
    session.add(message)
    await session.flush()
    return message


async def get_message(
    session: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID
) -> ChatMessage | None:
    result = await session.execute(
        select(ChatMessage).where(
            ChatMessage.id == message_id, ChatMessage.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def list_messages(
    session: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ChatMessage]:
    query = (
        select(ChatMessage)
        .where(
            ChatMessage.user_id == user_id,
            ChatMessage.conversation_id == conversation_id,
        )
        .order_by(ChatMessage.created_at.asc())
    )
    if limit is not None:
        query = query.limit(limit)
    if offset is not None:
        query = query.offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def create_message_citation(
    session: AsyncSession,
    user_id: uuid.UUID,
    chat_message_id: uuid.UUID,
    news_item_id: uuid.UUID,
) -> MessageCitation | None:
    """Create a citation linking a user's message to a user's news item.

    Returns ``None`` if either the message or the news item does not exist or
    is not owned by ``user_id``.
    """
    message = await session.execute(
        select(ChatMessage).where(
            ChatMessage.id == chat_message_id, ChatMessage.user_id == user_id
        )
    )
    if message.scalar_one_or_none() is None:
        return None

    news = await session.execute(
        select(NewsItem).where(NewsItem.id == news_item_id, NewsItem.user_id == user_id)
    )
    if news.scalar_one_or_none() is None:
        return None

    citation = MessageCitation(
        user_id=user_id,
        chat_message_id=chat_message_id,
        news_item_id=news_item_id,
    )
    session.add(citation)
    await session.flush()
    return citation


async def list_message_citations(
    session: AsyncSession,
    user_id: uuid.UUID,
    chat_message_id: uuid.UUID,
    limit: int | None = None,
    offset: int | None = None,
) -> list[MessageCitation]:
    query = (
        select(MessageCitation)
        .where(
            MessageCitation.user_id == user_id,
            MessageCitation.chat_message_id == chat_message_id,
        )
        .order_by(MessageCitation.created_at.asc())
    )
    if limit is not None:
        query = query.limit(limit)
    if offset is not None:
        query = query.offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def get_message_citation(
    session: AsyncSession, user_id: uuid.UUID, citation_id: uuid.UUID
) -> MessageCitation | None:
    result = await session.execute(
        select(MessageCitation).where(
            MessageCitation.id == citation_id, MessageCitation.user_id == user_id
        )
    )
    return result.scalar_one_or_none()
