"""Align the deployed schema with the declared models

Revision ID: 0005_schema_alignment
Revises: 0004_delivery_schedule
Create Date: 2026-10-28 01:10:00.000000

``alembic check`` (autogenerate comparison) reported three differences between
the models and the schema built by migration 0001. None of them changes
behaviour, but leaving them in place makes every future ``--autogenerate`` run
emit spurious operations, so they are reconciled once here:

* ``users.telegram_user_id`` was declared as ``unique=True, index=True`` in the
  model but created as a unique *constraint* plus a *second*, redundant plain
  index on the same column. The redundant index is dropped and the uniqueness
  is expressed as one unique index, which is exactly what the model declares.
* ``chat_messages.user_id`` and ``message_citations.user_id`` are declared with
  ``index=True`` but had no index. They are created here.

Uniqueness of ``telegram_user_id`` is never unenforced: the drop and the create
happen in one transaction, which PostgreSQL applies atomically.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_schema_alignment"
down_revision = "0004_delivery_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Model: ChatMessage.user_id(index=True)
    op.create_index(
        op.f("ix_chat_messages_user_id"), "chat_messages", ["user_id"], unique=False
    )
    # Model: MessageCitation.user_id(index=True)
    op.create_index(
        op.f("ix_message_citations_user_id"),
        "message_citations",
        ["user_id"],
        unique=False,
    )
    # Model: User.telegram_user_id(unique=True, index=True) -> one unique index.
    op.drop_constraint(op.f("uq_users_telegram_user_id"), "users", type_="unique")
    op.drop_index(op.f("ix_users_telegram_user_id"), table_name="users")
    op.create_index(
        op.f("ix_users_telegram_user_id"),
        "users",
        ["telegram_user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_users_telegram_user_id"), table_name="users")
    op.create_index(
        op.f("ix_users_telegram_user_id"),
        "users",
        ["telegram_user_id"],
        unique=False,
    )
    op.create_unique_constraint(
        op.f("uq_users_telegram_user_id"), "users", ["telegram_user_id"]
    )
    op.drop_index(op.f("ix_message_citations_user_id"), table_name="message_citations")
    op.drop_index(op.f("ix_chat_messages_user_id"), table_name="chat_messages")
