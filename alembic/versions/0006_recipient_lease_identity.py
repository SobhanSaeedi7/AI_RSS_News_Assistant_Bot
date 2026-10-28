"""Add recipient status, agent lease, and stable article identity

Revision ID: 0006_recipient_lease_identity
Revises: 0005_schema_alignment
Create Date: 2026-10-28 02:00:00.000000

Three additive changes, all with safe defaults for existing rows:

* ``users.delivery_unavailable_at`` records that Telegram reported this
  recipient as unavailable. NULL (every existing user) means "no known problem".
* ``users.agent_processing_until`` is a bounded, cross-process lease the bot
  holds while answering an ``/ask`` question. NULL means "not answering", so
  existing users are never blocked.
* ``news_items.external_id`` stores a publisher GUID/Atom id when the feed
  provides one. It is nullable, and the unique constraint allows any number of
  NULL values in PostgreSQL and SQLite, so feeds without ids keep working and
  existing rows need no backfill.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_recipient_lease_identity"
down_revision = "0005_schema_alignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("delivery_unavailable_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("agent_processing_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "news_items",
        sa.Column("external_id", sa.String(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_news_items_user_id_external_id",
        "news_items",
        ["user_id", "external_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_news_items_user_id_external_id", "news_items", type_="unique"
    )
    op.drop_column("news_items", "external_id")
    op.drop_column("users", "agent_processing_until")
    op.drop_column("users", "delivery_unavailable_at")
