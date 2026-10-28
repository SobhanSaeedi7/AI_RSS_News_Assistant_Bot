"""SQLAlchemy domain models for the AI RSS News Assistant.

Every user-owned record carries a ``user_id`` foreign key to ``users.id``.
Composite foreign keys enforce ownership relationships at the database level
so that, for example, a message can only reference a conversation belonging to
the same user, and a citation can only reference news belonging to the same
user. Cascade delete policies ensure feed removal never leaves orphaned news,
deliveries, or citations.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class User(Base):
    """A Telegram user of the bot."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    auto_delivery_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # Minimum seconds between two automatic deliveries for this user. ``0``
    # means "as soon as new items exist" (once per worker poll), which is the
    # behaviour before per-user scheduling existed. It never affects /news or
    # /ask, and ``auto_delivery_enabled = False`` overrides it entirely.
    delivery_interval_seconds: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when Telegram reports that this recipient cannot receive messages
    # (the user blocked the bot, or the private chat is unavailable). It is a
    # deliberately general status because a ``Forbidden`` response does not
    # prove the exact cause. Any later update from the user clears it.
    delivery_unavailable_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bounded, cross-process lease held by the bot while it is answering an
    # /ask question. The worker defers automatic delivery for this user until
    # the lease is released or expires, so a summary cannot interrupt an
    # in-progress answer. NULL means "not answering".
    agent_processing_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UserFeed(Base):
    """An RSS feed owned by a user."""

    __tablename__ = "user_feeds"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Health state used to back off a feed that keeps failing. ``last_error``
    # stores a fixed category ("unreachable", "not_a_feed", "processing"),
    # never exception text, so it is safe to log and to show in /listfeeds.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_fetch_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # A user cannot subscribe to the same feed URL twice.
        UniqueConstraint("user_id", "url", name="uq_user_feeds_user_id_url"),
        # Composite candidate key so news_items can reference (user_id, feed_id).
        UniqueConstraint("user_id", "id", name="uq_user_feeds_user_id_id"),
        Index("ix_user_feeds_user_id", "user_id"),
        Index("ix_user_feeds_user_id_active", "user_id", "active"),
        # The poll query filters on (active, next_fetch_at).
        Index("ix_user_feeds_active_next_fetch_at", "active", "next_fetch_at"),
    )


class NewsItem(Base):
    """A news item fetched from a feed, owned by the feed's user."""

    __tablename__ = "news_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    feed_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    normalized_url: Mapped[str] = mapped_column(String, nullable=False)
    # Stable publisher GUID/Atom id, when the feed provides one. It is the
    # preferred deduplication identity; normalized_url remains the fallback for
    # feeds that publish no id. NULL is allowed and never collides.
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # A news item belongs to the same user that owns its feed.
        ForeignKeyConstraint(
            ["user_id", "feed_id"],
            ["user_feeds.user_id", "user_feeds.id"],
            name="fk_news_items_user_feed",
            ondelete="CASCADE",
        ),
        # The same canonical article URL may only be stored once per user,
        # regardless of which feed it arrives through.
        UniqueConstraint(
            "user_id", "normalized_url", name="uq_news_items_user_id_normalized_url"
        ),
        # The same publisher GUID may only be stored once per user, even when
        # it appears in two feeds or with two different links.
        UniqueConstraint(
            "user_id", "external_id", name="uq_news_items_user_id_external_id"
        ),
        # Composite candidate key so message_citations can reference
        # (user_id, news_item_id).
        UniqueConstraint("user_id", "id", name="uq_news_items_user_id_id"),
        Index("ix_news_items_user_id", "user_id"),
        Index("ix_news_items_normalized_url", "normalized_url"),
        Index("ix_news_items_feed_id", "feed_id"),
        Index("ix_news_items_fetched_at", "fetched_at"),
    )


class NewsDelivery(Base):
    """A record that a news item was delivered to a user."""

    __tablename__ = "news_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    news_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # A delivery references news owned by the same user.
        ForeignKeyConstraint(
            ["user_id", "news_item_id"],
            ["news_items.user_id", "news_items.id"],
            name="fk_news_deliveries_user_news",
            ondelete="CASCADE",
        ),
        # A news item is delivered to a user at most once.
        UniqueConstraint(
            "user_id", "news_item_id", name="uq_news_deliveries_user_id_news_item_id"
        ),
        Index("ix_news_deliveries_user_id", "user_id"),
    )


class Conversation(Base):
    """A conversation between a user and the bot."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Composite candidate key so chat_messages can reference
        # (user_id, conversation_id).
        UniqueConstraint("user_id", "id", name="uq_conversations_user_id_id"),
        Index("ix_conversations_user_id", "user_id"),
    )


class ChatMessage(Base):
    """A message within a conversation."""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # A message belongs to the same user that owns its conversation.
        ForeignKeyConstraint(
            ["user_id", "conversation_id"],
            ["conversations.user_id", "conversations.id"],
            name="fk_chat_messages_user_conversation",
            ondelete="CASCADE",
        ),
        # Composite candidate key so message_citations can reference
        # (user_id, chat_message_id).
        UniqueConstraint("user_id", "id", name="uq_chat_messages_user_id_id"),
        Index("ix_chat_messages_conversation_id", "conversation_id"),
        Index("ix_chat_messages_user_id_created_at", "user_id", "created_at"),
    )


class MessageCitation(Base):
    """A citation linking a chat message to a news item."""

    __tablename__ = "message_citations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    chat_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    news_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # A citation references a message owned by the same user.
        ForeignKeyConstraint(
            ["user_id", "chat_message_id"],
            ["chat_messages.user_id", "chat_messages.id"],
            name="fk_message_citations_user_message",
            ondelete="CASCADE",
        ),
        # A citation references news owned by the same user.
        ForeignKeyConstraint(
            ["user_id", "news_item_id"],
            ["news_items.user_id", "news_items.id"],
            name="fk_message_citations_user_news",
            ondelete="CASCADE",
        ),
        Index("ix_message_citations_chat_message_id", "chat_message_id"),
        Index("ix_message_citations_news_item_id", "news_item_id"),
    )
