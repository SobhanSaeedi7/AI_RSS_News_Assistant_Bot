"""RSS feed ingestion tests.

Uses an in-memory SQLite async engine. All ingestion tests mock network
requests and feedparser; no real network or external feeds are used.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from ai_rss_news_assistant.feeds import (
    SafeRedirectHandler,
    safe_url_for_log,
    validate_feed_url,
)
from ai_rss_news_assistant.worker.ingestion import (
    ERROR_NOT_A_FEED,
    ERROR_UNREACHABLE,
    FEED_BACKOFF_MAX_SECONDS,
    ingest_feeds_for_user,
    next_retry_delay,
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
    external_id: str | None = None,
) -> dict:
    """Create a mock feedparser entry dict."""
    entry: dict = {}
    if include_link:
        entry["link"] = url
    if external_id is not None:
        entry["id"] = external_id
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
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
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
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
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
async def test_ingest_feeds_stores_the_publisher_external_id() -> None:
    """A stable publisher GUID is persisted with the article."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=221)
        await _add_feed(session, user, url="https://example.com/feed.xml")

        entry = _make_entry(
            url="https://example.com/article/1",
            title="With GUID",
            external_id="  publisher-guid-1  ",
        )
        parsed = _make_parsed_feed([entry])

        with (
            patch(
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
                return_value=parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["items_created"] == 1

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        stored = items.scalars().one()
        assert stored.external_id == "publisher-guid-1"
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_same_external_id_with_a_new_url_is_a_duplicate() -> None:
    """The publisher GUID wins when the same article moves to a new URL."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=222)
        feed = await _add_feed(session, user, url="https://example.com/feed.xml")

        existing = NewsItem(
            user_id=user.id,
            feed_id=feed.id,
            title="Existing",
            url="https://example.com/article/1",
            normalized_url="https://example.com/article/1",
            external_id="publisher-guid-1",
            fetched_at=datetime.now(UTC),
        )
        session.add(existing)
        await session.flush()

        # Same GUID, different (unseen) URL.
        entry = _make_entry(
            url="https://mirror.example.com/article/1",
            title="Moved Article",
            external_id="publisher-guid-1",
        )
        parsed = _make_parsed_feed([entry])

        with (
            patch(
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
                return_value=parsed,
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["items_created"] == 0
        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        assert len(items.scalars().all()) == 1
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_external_id_is_isolated_per_user() -> None:
    """Two users may store the same publisher GUID independently."""
    session = await _create_session()
    try:
        first = await _add_user(session, telegram_user_id=223)
        second = await _add_user(session, telegram_user_id=224)
        await _add_feed(session, first, url="https://a.example.com/feed.xml")
        await _add_feed(session, second, url="https://b.example.com/feed.xml")

        entry = _make_entry(
            url="https://example.com/article/1",
            title="Shared Article",
            external_id="publisher-guid-1",
        )
        parsed = _make_parsed_feed([entry])

        for user in (first, second):
            with (
                patch(
                    "ai_rss_news_assistant.feeds._open_url",
                    return_value=_make_feed_response([]),
                ),
                patch(
                    "ai_rss_news_assistant.feeds.feedparser.parse",
                    return_value=parsed,
                ),
            ):
                stats = await ingest_feeds_for_user(session, user.id)
            assert stats["items_created"] == 1

        for user in (first, second):
            items = await session.execute(
                select(NewsItem).where(NewsItem.user_id == user.id)
            )
            stored = items.scalars().all()
            assert len(stored) == 1
            assert stored[0].external_id == "publisher-guid-1"
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
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
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
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
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
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
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

        def _open_by_host(request, timeout=None):
            # Decide by URL, not by call order: feed iteration order is not
            # part of the contract, so a positional side_effect list would make
            # this test order-dependent.
            if "bad.example.com" in request.full_url:
                raise OSError("connection refused")
            return _make_feed_response([])

        with (
            patch(
                "ai_rss_news_assistant.feeds._open_url",
                side_effect=_open_by_host,
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
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
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
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
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
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
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
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
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
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
            "ai_rss_news_assistant.feeds._open_url",
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
            "ai_rss_news_assistant.feeds._open_url",
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
            "ai_rss_news_assistant.feeds._open_url",
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
        validate_feed_url(url)


def test_validate_url_rejects_embedded_credentials() -> None:
    """Feed credentials must not be stored or written to logs."""
    with pytest.raises(ValueError, match="credentials"):
        validate_feed_url("https://user:password@example.com/feed.xml")


def test_safe_url_for_log_strips_credentials_and_path() -> None:
    """Log labels contain only the host, never userinfo or path."""
    assert (
        safe_url_for_log("https://user:password@example.com/private/feed.xml")
        == "example.com"
    )


def test_redirect_handler_rejects_private_target_before_following() -> None:
    """A redirect to a private IP is rejected before urllib follows it."""
    handler = SafeRedirectHandler()

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
            "ai_rss_news_assistant.feeds._open_url",
            return_value=response,
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["feeds_processed"] == 1
        assert stats["items_created"] == 0
        assert len(stats["errors"]) == 1
    finally:
        await _close_session(session)


# --- feed health and backoff ----------------------------------------------


def test_next_retry_delay_doubles_and_is_capped() -> None:
    assert next_retry_delay(0) == 0
    assert next_retry_delay(1) == 300
    assert next_retry_delay(2) == 600
    assert next_retry_delay(3) == 1200
    # A feed broken for a very long time must not overflow or wait for years.
    assert next_retry_delay(50) == FEED_BACKOFF_MAX_SECONDS


@pytest.mark.anyio
async def test_successful_ingestion_records_health_and_last_fetch() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=951)
        feed = await _add_feed(session, user)
        feed.consecutive_failures = 4
        feed.last_error = ERROR_UNREACHABLE
        await session.flush()

        moment = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        with (
            patch(
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
                return_value=_make_parsed_feed([_make_entry()]),
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id, now=moment)

        assert stats["feeds_succeeded"] == 1
        assert feed.last_fetched_at == moment
        assert feed.consecutive_failures == 0
        assert feed.last_error is None
        assert feed.next_fetch_at is None
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_failed_fetch_records_failure_without_marking_success() -> None:
    """A failed fetch must not update last_fetched_at, and must back off."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=952)
        feed = await _add_feed(session, user)

        moment = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        with patch(
            "ai_rss_news_assistant.feeds._open_url",
            side_effect=OSError("connection refused"),
        ):
            stats = await ingest_feeds_for_user(session, user.id, now=moment)

        assert stats["feeds_succeeded"] == 0
        assert stats["errors"][0]["error"] == ERROR_UNREACHABLE
        assert feed.consecutive_failures == 1
        assert feed.last_error == ERROR_UNREACHABLE
        assert feed.last_fetched_at is None
        assert feed.next_fetch_at == moment + timedelta(seconds=300)

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        assert items.scalars().all() == []
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_unreadable_feed_is_recorded_as_not_a_feed() -> None:
    """An HTML response at poll time is an error, not a silent empty feed."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=953)
        feed = await _add_feed(session, user, url="https://example.com/page")

        with (
            patch(
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
                return_value={"version": "", "entries": [], "feed": {}},
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id)

        assert stats["items_created"] == 0
        assert stats["errors"][0]["error"] == ERROR_NOT_A_FEED
        assert feed.last_error == ERROR_NOT_A_FEED
        assert feed.consecutive_failures == 1
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_feed_in_backoff_is_skipped_until_it_is_due() -> None:
    """A failing feed is not hammered on every poll."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=954)
        feed = await _add_feed(session, user)
        moment = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        feed.next_fetch_at = moment + timedelta(seconds=600)
        await session.flush()

        with patch("ai_rss_news_assistant.feeds._open_url") as mock_open:
            stats = await ingest_feeds_for_user(session, user.id, now=moment)

        assert stats["feeds_processed"] == 0
        mock_open.assert_not_called()
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_recovered_feed_is_fetched_again_and_resets_failures() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=955)
        feed = await _add_feed(session, user)
        moment = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        feed.consecutive_failures = 2
        feed.last_error = ERROR_UNREACHABLE
        feed.next_fetch_at = moment - timedelta(seconds=1)
        await session.flush()

        with (
            patch(
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
                return_value=_make_parsed_feed([_make_entry()]),
            ),
        ):
            stats = await ingest_feeds_for_user(session, user.id, now=moment)

        assert stats["feeds_processed"] == 1
        assert stats["items_created"] == 1
        assert feed.consecutive_failures == 0
        assert feed.last_error is None
        assert feed.next_fetch_at is None
        assert feed.last_fetched_at == moment
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingestion_stores_the_feed_title_when_missing() -> None:
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=956)
        feed = await _add_feed(session, user)
        feed.title = None
        await session.flush()

        parsed = _make_parsed_feed([_make_entry()])
        parsed["feed"] = {"title": "  Real   Feed Name "}

        with (
            patch(
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
                return_value=parsed,
            ),
        ):
            await ingest_feeds_for_user(session, user.id)

        assert feed.title == "Real Feed Name"
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_first_fetch_imports_the_current_window_only_once() -> None:
    """First-run policy: a feed's current entries are imported, then deduplicated.

    There is no date cut-off on a feed's first fetch: whatever the feed
    currently publishes (its own archive window) is stored, and a second poll of
    the same document adds nothing.
    """
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=781)
        await _add_feed(session, user, url="https://example.com/feed.xml")

        entries = [
            _make_entry(url=f"https://example.com/article/{n}", title=f"Article {n}")
            for n in range(3)
        ]
        parsed = _make_parsed_feed(entries)

        with (
            patch(
                "ai_rss_news_assistant.feeds._open_url",
                return_value=_make_feed_response(),
            ),
            patch(
                "ai_rss_news_assistant.feeds.feedparser.parse",
                return_value=parsed,
            ),
        ):
            first = await ingest_feeds_for_user(session, user.id)
            second = await ingest_feeds_for_user(session, user.id)

        assert first["items_created"] == 3
        assert first["feeds_succeeded"] == 1
        assert second["items_created"] == 0
        assert second["feeds_succeeded"] == 1

        stored = (
            (await session.execute(select(NewsItem).where(NewsItem.user_id == user.id)))
            .scalars()
            .all()
        )
        assert len(stored) == 3
    finally:
        await _close_session(session)
