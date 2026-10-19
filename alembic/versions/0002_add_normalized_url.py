"""Add normalized_url to news_items for cross-feed deduplication

Revision ID: 0002_add_normalized_url
Revises: 0001_initial_schema
Create Date: 2026-10-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from ai_rss_news_assistant.db.url_normalization import normalize_url
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_add_normalized_url"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

_OLD_UNIQUE_NAME = "uq_news_items_feed_id_url"
_NEW_UNIQUE_NAME = "uq_news_items_user_id_normalized_url"


def upgrade() -> None:
    # Add normalized_url as nullable so existing rows can be backfilled.
    op.add_column(
        "news_items",
        sa.Column("normalized_url", sa.String(), nullable=True),
    )

    # Backfill existing rows: normalize each stored url.
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, url FROM news_items")).fetchall()
    for row_id, url in rows:
        normalized = normalize_url(url)
        connection.execute(
            sa.text(
                "UPDATE news_items SET normalized_url = :normalized WHERE id = :id"
            ),
            {"normalized": normalized, "id": row_id},
        )

    # Enforce non-null after backfill.
    op.alter_column("news_items", "normalized_url", nullable=False)

    # Replace per-feed URL uniqueness with per-user canonical URL uniqueness.
    op.drop_constraint(_OLD_UNIQUE_NAME, "news_items", type_="unique")
    op.create_unique_constraint(
        _NEW_UNIQUE_NAME,
        "news_items",
        ["user_id", "normalized_url"],
    )
    op.create_index(
        "ix_news_items_normalized_url",
        "news_items",
        ["normalized_url"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_UNIQUE_NAME, "news_items", type_="unique")
    op.drop_index("ix_news_items_normalized_url", table_name="news_items")
    op.create_unique_constraint(
        _OLD_UNIQUE_NAME,
        "news_items",
        ["feed_id", "url"],
    )
    op.drop_column("news_items", "normalized_url")
