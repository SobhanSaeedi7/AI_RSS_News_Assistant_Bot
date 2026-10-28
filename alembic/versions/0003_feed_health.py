"""Add feed health state for controlled backoff

Revision ID: 0003_feed_health
Revises: 0002_add_normalized_url
Create Date: 2026-10-28 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_feed_health"
down_revision = "0002_add_normalized_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows are healthy by definition: they have never recorded a
    # failure, and a NULL next_fetch_at means "due at the next poll".
    op.add_column(
        "user_feeds",
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "user_feeds",
        sa.Column("last_error", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "user_feeds",
        sa.Column("next_fetch_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_user_feeds_active_next_fetch_at",
        "user_feeds",
        ["active", "next_fetch_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_user_feeds_active_next_fetch_at", table_name="user_feeds")
    op.drop_column("user_feeds", "next_fetch_at")
    op.drop_column("user_feeds", "last_error")
    op.drop_column("user_feeds", "consecutive_failures")
