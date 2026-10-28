"""Add per-user automatic delivery scheduling

Revision ID: 0004_delivery_schedule
Revises: 0003_feed_health
Create Date: 2026-10-28 00:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_delivery_schedule"
down_revision = "0003_feed_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0 means "no throttle": deliver as soon as undelivered items exist, which
    # is exactly how every existing user behaved before this migration.
    op.add_column(
        "users",
        sa.Column(
            "delivery_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # NULL means "never delivered automatically yet", so the first run is due
    # immediately regardless of the chosen interval.
    op.add_column(
        "users",
        sa.Column("last_delivered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_delivered_at")
    op.drop_column("users", "delivery_interval_seconds")
