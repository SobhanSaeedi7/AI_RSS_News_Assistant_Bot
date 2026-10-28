"""Tests for SQLAlchemy model definitions.

These tests inspect model metadata (columns, types, foreign keys, unique
constraints, indexes) without connecting to a database. They verify that the
schema is declared correctly before any integration testing.
"""

from __future__ import annotations

from sqlalchemy import DateTime, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from ai_rss_news_assistant.db.models import (
    Base,
)


def _table(name: str):
    return Base.metadata.tables[name]


def test_all_seven_tables_registered() -> None:
    expected = {
        "users",
        "user_feeds",
        "news_items",
        "news_deliveries",
        "conversations",
        "chat_messages",
        "message_citations",
    }
    assert set(Base.metadata.tables.keys()) == expected


def test_users_columns() -> None:
    t = _table("users")
    cols = {c.name: c for c in t.columns}
    assert cols["id"].type.__class__ is UUID
    assert cols["telegram_user_id"].nullable is False
    assert cols["auto_delivery_enabled"].nullable is False
    assert cols["is_active"].nullable is False
    assert isinstance(cols["created_at"].type, DateTime)
    assert isinstance(cols["updated_at"].type, DateTime)


def test_users_delivery_schedule_columns() -> None:
    """The per-user schedule is stored on the user row, not in worker memory."""
    t = _table("users")
    cols = {c.name: c for c in t.columns}

    assert cols["delivery_interval_seconds"].nullable is False
    assert cols["last_delivered_at"].nullable is True
    assert cols["auto_delivery_enabled"].nullable is False


def test_users_recipient_and_agent_coordination_columns() -> None:
    """Recipient status and the /ask lease are nullable per-user state."""
    t = _table("users")
    cols = {c.name: c for c in t.columns}

    assert isinstance(cols["delivery_unavailable_at"].type, DateTime)
    assert cols["delivery_unavailable_at"].nullable is True
    assert isinstance(cols["agent_processing_until"].type, DateTime)
    assert cols["agent_processing_until"].nullable is True


def test_users_telegram_user_id_unique() -> None:
    t = _table("users")
    assert t.c.telegram_user_id.unique is True
    unique_indexes = [
        idx
        for idx in t.indexes
        if idx.unique and list(idx.columns) == [t.c.telegram_user_id]
    ]
    assert len(unique_indexes) == 1


def test_user_feeds_columns_and_fk() -> None:
    t = _table("user_feeds")
    cols = {c.name: c for c in t.columns}
    assert cols["user_id"].nullable is False
    assert cols["url"].nullable is False
    assert cols["title"].nullable is True
    assert cols["last_fetched_at"].nullable is True
    assert cols["active"].nullable is False

    fks = [fk for fk in t.foreign_keys if fk.parent.name == "user_id"]
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"
    assert fks[0].target_fullname == "users.id"


def test_user_feeds_unique_constraints() -> None:
    t = _table("user_feeds")
    uniques = [
        [c.name for c in uc.columns]
        for uc in t.constraints
        if isinstance(uc, UniqueConstraint)
    ]
    assert ["user_id", "url"] in uniques
    assert ["user_id", "id"] in uniques  # composite candidate key for FK refs


def test_news_items_composite_fk_enforces_ownership() -> None:
    t = _table("news_items")
    # Also check the ForeignKeyConstraint object directly
    fk_constraints = [c for c in t.constraints if isinstance(c, ForeignKeyConstraint)]
    composite = [
        c for c in fk_constraints if list(c.columns) == [t.c.user_id, t.c.feed_id]
    ]
    assert len(composite) == 1
    assert composite[0].ondelete == "CASCADE"


def test_news_items_unique_constraints() -> None:
    t = _table("news_items")
    uniques = [
        [c.name for c in uc.columns]
        for uc in t.constraints
        if isinstance(uc, UniqueConstraint)
    ]
    assert ["user_id", "normalized_url"] in uniques
    assert ["user_id", "external_id"] in uniques
    assert ["user_id", "id"] in uniques


def test_news_items_external_id_is_optional() -> None:
    """A feed without a publisher GUID must keep working without a backfill."""
    t = _table("news_items")
    assert t.c.external_id.nullable is True


def test_news_deliveries_composite_fk() -> None:
    t = _table("news_deliveries")
    fk_constraints = [c for c in t.constraints if isinstance(c, ForeignKeyConstraint)]
    composite = [
        c for c in fk_constraints if list(c.columns) == [t.c.user_id, t.c.news_item_id]
    ]
    assert len(composite) == 1
    assert composite[0].ondelete == "CASCADE"

    uniques = [
        [c.name for c in uc.columns]
        for uc in t.constraints
        if isinstance(uc, UniqueConstraint)
    ]
    assert ["user_id", "news_item_id"] in uniques


def test_conversations_unique_for_composite_fk_ref() -> None:
    t = _table("conversations")
    uniques = [
        [c.name for c in uc.columns]
        for uc in t.constraints
        if isinstance(uc, UniqueConstraint)
    ]
    assert ["user_id", "id"] in uniques


def test_chat_messages_composite_fk() -> None:
    t = _table("chat_messages")
    fk_constraints = [c for c in t.constraints if isinstance(c, ForeignKeyConstraint)]
    composite = [
        c
        for c in fk_constraints
        if list(c.columns) == [t.c.user_id, t.c.conversation_id]
    ]
    assert len(composite) == 1
    assert composite[0].ondelete == "CASCADE"

    uniques = [
        [c.name for c in uc.columns]
        for uc in t.constraints
        if isinstance(uc, UniqueConstraint)
    ]
    assert ["user_id", "id"] in uniques


def test_message_citations_dual_composite_fk() -> None:
    t = _table("message_citations")
    fk_constraints = [c for c in t.constraints if isinstance(c, ForeignKeyConstraint)]
    msg_composite = [
        c
        for c in fk_constraints
        if list(c.columns) == [t.c.user_id, t.c.chat_message_id]
    ]
    news_composite = [
        c for c in fk_constraints if list(c.columns) == [t.c.user_id, t.c.news_item_id]
    ]
    assert len(msg_composite) == 1
    assert len(news_composite) == 1
    assert msg_composite[0].ondelete == "CASCADE"
    assert news_composite[0].ondelete == "CASCADE"


def test_all_user_owned_tables_have_user_id_fk() -> None:
    """Every user-owned table links to users.id with CASCADE delete."""
    for table_name in [
        "user_feeds",
        "news_items",
        "news_deliveries",
        "conversations",
        "chat_messages",
        "message_citations",
    ]:
        t = _table(table_name)
        user_fks = [
            fk
            for fk in t.foreign_keys
            if fk.parent.name == "user_id" and fk.target_fullname == "users.id"
        ]
        assert len(user_fks) == 1, f"{table_name} missing user_id -> users.id FK"
        assert user_fks[0].ondelete == "CASCADE", (
            f"{table_name} user_id FK missing CASCADE"
        )


def test_uuid_primary_keys() -> None:
    """All tables use UUID primary keys."""
    for table in Base.metadata.tables.values():
        pk_cols = [c.name for c in table.primary_key.columns]
        assert pk_cols == ["id"], f"{table.name} PK is {pk_cols}"
        assert table.c.id.type.__class__ is UUID
