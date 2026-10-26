"""RSS feed ingestion tests.

Uses an in-memory SQLite async engine. All ingestion tests mock network
requests and feedparser; no real network or external feeds are used.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ai_rss_news_assistant.db.base import Base
from ai_rss_news_assistant.db.models import NewsItem, User, UserFeed
from ai_rss_news_assistant.db.repository import create_news_item
from ai_rss_news_assistant.db.rss_ingestion import (
    _safe_url_for_log,
    _SafeRedirectHandler,
    _validate_url,
    ingest_feeds_for_user,
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


async def _create_session() -> AsyncSession:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    session._test_engine = engine  # type: ignore[attr-defined]
    return session


async def _close_session(session: AsyncSession) -> None:
    await session.close()
    engine = getattr(session, "_test_engine", None)
    if engine is not None:
        await engine.dispose()


async def _add_user(session: AsyncSession, telegram_user_id: int = 111) -> User:
    user = User(telegram_user_id=telegram_user_id)
    session.add(user)
    await session.flush()
    return user


async def _add_feed(
    session: AsyncSession,
    user: User,
    url: str = "https://example.com/feed.xml",
    active: bool = True,
) -> UserFeed:
    feed = UserFeed(user_id=user.id, url=url, title="Example Feed", active=active)
    session.add(feed)
    await session.flush()
    return feed


class _FakeResponse:
    """Simple mock HTTP response with a geturl() method."""

    def __init__(
        self,
        content: bytes = b"<rss><channel></channel></rss>",
        url: str = "https://example.com/feed.xml",
    ):
        self._content = content
        self._url = url

    def read(self, size: int = -1) -> bytes:
        return self._content[:size] if size >= 0 else self._content

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _make_feed_response(*args, **kwargs) -> _FakeResponse:
    """Create a mock HTTP response with feed content."""
    return _FakeResponse()


def _make_parsed_feed(entries: list[dict]) -> dict:
    """Create a mock parsed feed with entries."""
    return {"entries": entries}


def _make_entry(
    url: str = "https://example.com/article/1",
    title: str = "Test Article",
    summary: str = "Test summary",
    content: str | None = None,
    published_parsed: tuple | None = None,
    include_link: bool = True,
) -> dict:
    """Create a mock feedparser entry dict."""
    entry: dict = {}
    if include_link:
        entry["link"] = url
    entry["title"] = title
    if summary is not None:
        entry["summary"] = summary
    if content is not None:
        entry["content"] = [{"value": content}]
    if published_parsed is not None:
        entry["published_parsed"] = published_parsed
    return entry


@pytest.mark.anyio
async def test_ingest_feeds_success() -> None:
    """Successful ingestion creates news items from feed entries."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=111)
        feed = await _add_feed(session, user, url="https://example.com/feed.xml")

        entry = _make_entry(
            url="https://example.com/article/1",
            title="Test Article",
            summary="A test article",
        )
        parsed = _make_parsed_feed([entry])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                return_value=parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["feeds_processed"] == 1
        assert stats["items_created"] == 1
        assert stats["errors"] == []

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        db_items = items.scalars().all()
        assert len(db_items) == 1
        assert db_items[0].title == "Test Article"
        assert db_items[0].summary == "A test article"
        assert db_items[0].url == "https://example.com/article/1"
        assert db_items[0].normalized_url == "https://example.com/article/1"
        assert db_items[0].feed_id == feed.id
        assert db_items[0].user_id == user.id
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_duplicate_not_created() -> None:
    """Same normalized URL for same user is not inserted twice."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=211)
        feed = await _add_feed(session, user, url="https://example.com/feed.xml")

        # Pre-existing news item with same normalized URL
        existing = NewsItem(
            user_id=user.id,
            feed_id=feed.id,
            title="Existing",
            url="https://example.com/article/1",
            normalized_url="https://example.com/article/1",
            fetched_at=datetime.now(UTC),
        )
        session.add(existing)
        await session.flush()

        entry = _make_entry(
            url="https://example.com/article/1",
            title="Duplicate Article",
        )
        parsed = _make_parsed_feed([entry])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                return_value=parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["items_created"] == 0
        assert stats["feeds_processed"] == 1

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        assert len(items.scalars().all()) == 1
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_skips_entry_without_url() -> None:
    """Entries without a usable URL are skipped."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=311)
        await _add_feed(session, user, url="https://example.com/feed.xml")

        entry = _make_entry(url="https://example.com/article/1", title="Valid Article")
        entry_no_url = {"title": "No URL Article"}  # no link or id
        parsed = _make_parsed_feed([entry_no_url, entry])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                return_value=parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["items_created"] == 1
        assert stats["errors"] == []

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        assert len(items.scalars().all()) == 1
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_skips_malformed_article_url() -> None:
    """A malformed article URL does not abort later entries in the same feed."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=312)
        await _add_feed(session, user, url="https://example.com/feed.xml")

        malformed = _make_entry(url="http://[::1", title="Malformed Article")
        valid = _make_entry(url="https://example.com/article/1", title="Valid Article")
        parsed = _make_parsed_feed([malformed, valid])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                return_value=parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["items_created"] == 1
        assert stats["errors"] == []

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        db_items = items.scalars().all()
        assert len(db_items) == 1
        assert db_items[0].title == "Valid Article"
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_missing_fields() -> None:
    """Entries with missing optional fields are still ingested."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=411)
        await _add_feed(session, user, url="https://example.com/feed.xml")

        entry = _make_entry(
            url="https://example.com/article/1",
            title="",
            summary=None,
            content=None,
            published_parsed=None,
        )
        parsed = _make_parsed_feed([entry])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                return_value=parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["items_created"] == 1

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        db_items = items.scalars().all()
        assert len(db_items) == 1
        assert db_items[0].title == ""
        assert db_items[0].summary is None
        assert db_items[0].content is None
        assert db_items[0].published_at is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_request_failure_isolated() -> None:
    """A feed fetch failure does not prevent other feeds from being processed."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=511)
        await _add_feed(session, user, url="https://good.example.com/feed.xml")
        bad_feed = await _add_feed(
            session, user, url="https://bad.example.com/feed.xml"
        )

        good_entry = _make_entry(
            url="https://good.example.com/article/1",
            title="Good Article",
        )
        good_parsed = _make_parsed_feed([good_entry])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion._open_url",
                side_effect=[
                    _make_feed_response([]),  # good feed response
                    OSError("connection refused"),  # bad feed raises error
                ],
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                return_value=good_parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["feeds_processed"] == 2
        assert stats["items_created"] == 1
        assert len(stats["errors"]) == 1
        assert stats["errors"][0]["feed_id"] == str(bad_feed.id)
        assert stats["errors"][0]["feed_url"] == "https://bad.example.com/feed.xml"

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        db_items = items.scalars().all()
        assert len(db_items) == 1
        assert db_items[0].title == "Good Article"
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_user_isolation() -> None:
    """User A cannot see or ingest user B's feeds."""
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=611)
        user_b = await _add_user(session, telegram_user_id=711)
        await _add_feed(session, user_a, url="https://a.example.com/feed.xml")
        await _add_feed(session, user_b, url="https://b.example.com/feed.xml")

        entry_a = _make_entry(url="https://a.example.com/article/1", title="Article A")
        entry_b = _make_entry(url="https://b.example.com/article/1", title="Article B")

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                side_effect=[
                    _make_parsed_feed([entry_a]),
                    _make_parsed_feed([entry_b]),
                ],
            ),
        ):
            stats_a = await ingest_feeds_for_user(session, user_a.id)
            stats_b = await ingest_feeds_for_user(session, user_b.id)

        assert stats_a["items_created"] == 1
        assert stats_b["items_created"] == 1

        items_a = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user_a.id)
        )
        items_b = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user_b.id)
        )
        db_items_a = items_a.scalars().all()
        db_items_b = items_b.scalars().all()
        assert len(db_items_a) == 1
        assert len(db_items_b) == 1
        assert db_items_a[0].title == "Article A"
        assert db_items_b[0].title == "Article B"
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_inactive_feed_skipped() -> None:
    """Inactive feeds are not polled."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=711)
        active_feed = await _add_feed(
            session, user, url="https://active.example.com/feed.xml", active=True
        )
        await _add_feed(
            session, user, url="https://inactive.example.com/feed.xml", active=False
        )

        entry = _make_entry(
            url="https://active.example.com/article/1",
            title="Active Article",
        )
        parsed = _make_parsed_feed([entry])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                return_value=parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["feeds_processed"] == 1
        assert stats["items_created"] == 1
        assert stats["errors"] == []

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        db_items = items.scalars().all()
        assert len(db_items) == 1
        assert db_items[0].feed_id == active_feed.id
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_integrity_conflict_does_not_block_later_articles() -> None:
    """Duplicate URL on one article does not prevent later articles in same feed."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=811)
        feed = await _add_feed(session, user, url="https://example.com/feed.xml")

        # Pre-existing article with same normalized URL
        existing = NewsItem(
            user_id=user.id,
            feed_id=feed.id,
            title="Existing",
            url="https://example.com/existing/1",
            normalized_url="https://example.com/existing/1",
            fetched_at=datetime.now(UTC),
        )
        session.add(existing)
        await session.flush()

        # First entry: duplicate URL (should be skipped)
        # Second entry: different URL (should be created)
        entry_dup = _make_entry(
            url="https://example.com/existing/1",
            title="Duplicate Article",
        )
        entry_new = _make_entry(
            url="https://example.com/article/2",
            title="New Article",
        )
        parsed = _make_parsed_feed([entry_dup, entry_new])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                return_value=parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["items_created"] == 1
        assert stats["errors"] == []

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        db_items = items.scalars().all()
        assert len(db_items) == 2  # existing + new
        titles = {i.title for i in db_items}
        assert titles == {"Existing", "New Article"}
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_integrity_conflict_preserves_transaction() -> None:
    """IntegrityError on one article does not discard earlier inserts."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=821)
        feed = await _add_feed(session, user, url="https://example.com/feed.xml")

        # First article created successfully
        await create_news_item(
            session,
            user_id=user.id,
            feed_id=feed.id,
            url="https://example.com/article/1",
            title="First Article",
            normalized_url="https://example.com/article/1",
            fetched_at=datetime.now(UTC),
        )
        await session.flush()

        # Second entry: duplicate URL â†’ IntegrityError
        # Third entry: different URL â†’ should still be created
        entry_dup = _make_entry(
            url="https://example.com/article/1",
            title="Duplicate",
        )
        entry_new = _make_entry(
            url="https://example.com/article/3",
            title="Third Article",
        )
        parsed = _make_parsed_feed([entry_dup, entry_new])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                return_value=parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["items_created"] == 1  # Only the third article

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id).order_by(NewsItem.title)
        )
        db_items = items.scalars().all()
        assert len(db_items) == 2  # first + third
        titles = {i.title for i in db_items}
        assert titles == {"First Article", "Third Article"}
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_oversized_response_rejected() -> None:
    """Oversized HTTP response is rejected without processing."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=911)
        feed = await _add_feed(session, user, url="https://example.com/feed.xml")

        # Response larger than MAX_RESPONSE_BYTES
        oversized_body = b"x" * (5 * 1024 * 1024 + 1)
        response = MagicMock()
        response.read = MagicMock(return_value=oversized_body)

        with patch(
            "ai_rss_news_assistant.db.rss_ingestion._open_url",
            return_value=response,
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["feeds_processed"] == 1
        assert stats["items_created"] == 0
        assert len(stats["errors"]) == 1
        assert stats["errors"][0]["feed_id"] == str(feed.id)
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_unsupported_scheme_rejected() -> None:
    """Non-http(s) schemes are rejected."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=921)
        await _add_feed(session, user, url="ftp://example.com/feed.xml")

        with patch(
            "ai_rss_news_assistant.db.rss_ingestion._open_url",
            return_value=_make_feed_response([]),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["feeds_processed"] == 1
        assert stats["items_created"] == 0
        assert len(stats["errors"]) == 1
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_private_ip_rejected() -> None:
    """Private IP addresses are rejected."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=931)
        await _add_feed(session, user, url="http://127.0.0.1/feed.xml")

        with patch(
            "ai_rss_news_assistant.db.rss_ingestion._open_url",
            return_value=_make_feed_response([]),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["feeds_processed"] == 1
        assert stats["items_created"] == 0
        assert len(stats["errors"]) == 1
    finally:
        await _close_session(session)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.0.1/feed.xml",
        "http://192.168.255.254/feed.xml",
        "http://100.64.0.1/feed.xml",
        "http://198.18.0.1/feed.xml",
    ],
)
def test_validate_url_blocks_private_and_reserved_ranges(url: str) -> None:
    """All private/reserved IPv4 ranges are rejected, not only loopback."""
    with pytest.raises(ValueError, match="not allowed"):
        _validate_url(url)


def test_validate_url_rejects_embedded_credentials() -> None:
    """Feed credentials must not be stored or written to logs."""
    with pytest.raises(ValueError, match="credentials"):
        _validate_url("https://user:password@example.com/feed.xml")


def test_safe_url_for_log_strips_credentials_and_path() -> None:
    """Log labels contain only the host, never userinfo or path."""
    assert (
        _safe_url_for_log("https://user:password@example.com/private/feed.xml")
        == "example.com"
    )


def test_redirect_handler_rejects_private_target_before_following() -> None:
    """A redirect to a private IP is rejected before urllib follows it."""
    handler = _SafeRedirectHandler()

    with pytest.raises(ValueError, match="not allowed"):
        handler.redirect_request(
            MagicMock(),
            MagicMock(),
            302,
            "Found",
            {},
            "http://192.168.1.1/internal",
        )


@pytest.mark.anyio
async def test_ingest_feeds_redirect_to_private_ip_rejected() -> None:
    """Redirect to a private IP is rejected."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=941)
        await _add_feed(session, user, url="https://example.com/feed.xml")

        response = MagicMock()
        response.geturl = MagicMock(return_value="http://127.0.0.1/internal")
        response.read = MagicMock(return_value=b"<rss><channel></channel></rss>")

        with patch(
            "ai_rss_news_assistant.db.rss_ingestion._open_url",
            return_value=response,
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["feeds_processed"] == 1
        assert stats["items_created"] == 0
        assert len(stats["errors"]) == 1
    finally:
        await _close_session(session)
