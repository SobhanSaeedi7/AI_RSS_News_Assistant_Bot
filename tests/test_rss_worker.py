"""RSS worker tests.

Uses an in-memory SQLite async engine. All tests mock network
requests and feedparser; no real network or external feeds are used.
"""

from __future__ import annotations

import asyncio
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
from ai_rss_news_assistant.db.rss_ingestion import ingest_feeds_for_user
from ai_rss_news_assistant.db.rss_worker import NewsWorker

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


async def _add_news_item(
    session: AsyncSession,
    user: User,
    feed: UserFeed,
    url: str = "https://example.com/article/1",
    content: str | None = None,
    summary: str | None = "A summary",
) -> NewsItem:
    item = NewsItem(
        user_id=user.id,
        feed_id=feed.id,
        title="Example Article",
        url=url,
        normalized_url=url,
        content=content,
        summary=summary,
        fetched_at=datetime.now(UTC),
    )
    session.add(item)
    await session.flush()
    return item


def _make_feed_response(
    content: bytes = b"<rss><channel></channel></rss>",
) -> MagicMock:
    response = MagicMock()
    response.read = MagicMock(return_value=content)
    response.geturl = lambda: "https://example.com/feed.xml"
    response.__enter__.return_value = response

    return response


def _make_parsed_feed(entries: list[dict]) -> dict:
    return {"entries": entries}


def _make_entry(
    url: str = "https://example.com/article/1",
    title: str = "Test Article",
    summary: str = "Test summary",
    content: str | None = None,
    published_parsed: tuple | None = None,
) -> dict:
    entry: dict = {}
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
async def test_ingest_feeds_for_user_creates_news_items() -> None:
    """Worker ingests news from an active feed owned by the user."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=111)
        await _add_feed(session, user, url="https://example.com/feed.xml")

        entry = _make_entry(
            url="https://example.com/article/1",
            title="Test Article",
            summary="A test article",
        )
        parsed = _make_parsed_feed([entry])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.urllib.request.urlopen",
                return_value=_make_feed_response(),
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
        assert db_items[0].title == "Test Article"
        assert db_items[0].summary == "A test article"
        assert db_items[0].user_id == user.id
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_ingest_feeds_skips_inactive_feeds() -> None:
    """Inactive feeds are not polled."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=211)
        await _add_feed(
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
                "ai_rss_news_assistant.db.rss_ingestion.urllib.request.urlopen",
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
        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        assert len(items.scalars().all()) == 1
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_worker_polls_and_processes_feeds() -> None:
    """Worker polls all users with active feeds and creates news items."""
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=611)
        user_b = await _add_user(session, telegram_user_id=711)
        await _add_feed(session, user_a, url="https://a.example.com/feed.xml")
        await _add_feed(session, user_b, url="https://b.example.com/feed.xml")
        await session.commit()

        entry_a = _make_entry(url="https://a.example.com/article/1", title="Article A")
        entry_b = _make_entry(url="https://b.example.com/article/1", title="Article B")

        def urlopen_side_effect(req, timeout=None):
            if "a.example.com" in req.full_url:
                return _make_feed_response(b"feed_a")
            return _make_feed_response(b"feed_b")

        def feedparser_side_effect(content: bytes) -> dict:
            if content == b"feed_a":
                return _make_parsed_feed([entry_a])
            return _make_parsed_feed([entry_b])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.urllib.request.urlopen",
                side_effect=urlopen_side_effect,
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                side_effect=feedparser_side_effect,
            ),
        ):
            worker = NewsWorker(
                engine=session.bind,  # type: ignore[arg-type]
                poll_interval_seconds=1,
                fetch_full_text=False,
            )
            await worker._poll_once()

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
async def test_ingest_feeds_isolates_user_failures() -> None:
    """A failing feed for one user does not prevent another user's feeds."""
    session = await _create_session()
    try:
        user_a = await _add_user(session, telegram_user_id=611)
        user_b = await _add_user(session, telegram_user_id=711)
        await _add_feed(session, user_a, url="https://a.example.com/feed.xml")
        await _add_feed(session, user_b, url="https://b.example.com/feed.xml")
        await session.commit()

        entry_a = _make_entry(url="https://a.example.com/article/1", title="Article A")
        entry_b = _make_entry(url="https://b.example.com/article/1", title="Article B")

        def urlopen_side_effect(req, timeout=None):
            if "a.example.com" in req.full_url:
                raise OSError("connection refused")
            return _make_feed_response(b"feed_b")

        def feedparser_side_effect(content: bytes) -> dict:
            if content == b"feed_a":
                return _make_parsed_feed([entry_a])
            return _make_parsed_feed([entry_b])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.urllib.request.urlopen",
                side_effect=urlopen_side_effect,
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                side_effect=feedparser_side_effect,
            ),
        ):
            stats_a = await ingest_feeds_for_user(session, user_a.id)
            stats_b = await ingest_feeds_for_user(session, user_b.id)

        assert stats_a["items_created"] == 0
        assert len(stats_a["errors"]) == 1
        assert stats_b["items_created"] == 1
        assert stats_b["errors"] == []

        items_a = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user_a.id)
        )
        items_b = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user_b.id)
        )
        db_items_a = items_a.scalars().all()
        db_items_b = items_b.scalars().all()
        assert len(db_items_a) == 0
        assert len(db_items_b) == 1
        assert db_items_b[0].title == "Article B"
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_extract_text_strips_scripts_and_styles() -> None:
    """HTML parser strips script/style tags and returns clean text."""
    from ai_rss_news_assistant.db.html_text_extractor import html_to_text

    html = """
    <html><head><title>Test</title></head>
    <body>
        <script>alert('evil')</script>
        <style>.foo { color: red; }</style>
        <p>Hello <b>world</b></p>
        <div><span>Nested text</span></div>
    </body></html>
    """
    text = html_to_text(html)
    assert "alert" not in text
    assert "color: red" not in text
    assert "Hello world" in text
    assert "Nested text" in text


@pytest.mark.anyio
async def test_extract_text_empty_input() -> None:
    """Empty or whitespace-only HTML returns empty string."""
    from ai_rss_news_assistant.db.html_text_extractor import html_to_text

    assert html_to_text("") == ""
    assert html_to_text("   ") == ""
    assert html_to_text("<html></html>") == ""


@pytest.mark.anyio
async def test_ingest_feeds_request_failure_isolated() -> None:
    """A feed fetch failure does not prevent other feeds from being processed."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=511)
        await _add_feed(session, user, url="https://good.example.com/feed.xml")
        await _add_feed(session, user, url="https://bad.example.com/feed.xml")

        good_entry = _make_entry(
            url="https://good.example.com/article/1",
            title="Good Article",
        )
        good_parsed = _make_parsed_feed([good_entry])

        def urlopen_side_effect(req, timeout=None):
            if "bad.example.com" in req.full_url:
                raise OSError("connection refused")
            return _make_feed_response([])

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.urllib.request.urlopen",
                side_effect=urlopen_side_effect,
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

        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        db_items = items.scalars().all()
        assert len(db_items) == 1
        assert db_items[0].title == "Good Article"
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_fetch_full_text_preserves_summary_on_failure() -> None:
    """When full-text extraction fails, the RSS summary is preserved."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=611)
        feed = await _add_feed(session, user, url="https://example.com/feed.xml")
        item = await _add_news_item(
            session,
            user,
            feed,
            url="https://example.com/article/1",
            content=None,
            summary="Original summary",
        )

        # Simulate a failed fetch
        with patch(
            "ai_rss_news_assistant.db.rss_worker._fetch_feed_content",
            side_effect=OSError("connection refused"),
        ):
            worker = NewsWorker(
                engine=session.bind,  # type: ignore[arg-type]
                poll_interval_seconds=1,
                fetch_full_text=True,
                fetch_timeout=5,
            )
            await worker._fetch_missing_full_text(session, user.id)

        item = await session.get(NewsItem, item.id)
        assert item.content is None
        assert item.summary == "Original summary"
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_fetch_full_text_updates_content() -> None:
    """Full text is extracted and stored for news items without content."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=711)
        feed = await _add_feed(session, user, url="https://example.com/feed.xml")
        item = await _add_news_item(
            session,
            user,
            feed,
            url="https://example.com/article/1",
            content=None,
            summary=None,
        )

        html = "<html><body><p>Full article text here.</p></body>"
        response = MagicMock()
        response.read = MagicMock(return_value=html.encode("utf-8"))
        response.geturl = MagicMock(return_value="https://example.com/article/1")

        with patch(
            "ai_rss_news_assistant.db.rss_worker._fetch_feed_content",
            return_value=html.encode("utf-8"),
        ):
            worker = NewsWorker(
                engine=session.bind,  # type: ignore[arg-type]
                poll_interval_seconds=1,
                fetch_full_text=True,
                fetch_timeout=5,
            )
            await worker._fetch_missing_full_text(session, user.id)

        item = await session.get(NewsItem, item.id)
        assert item.content == "Full article text here."
    finally:
        await _close_session(session)


@pytest.mark.anyio
async def test_worker_stop_gracefully() -> None:
    """Worker stops when stop() is called and processing occurs before shutdown."""
    session = await _create_session()
    try:
        user = await _add_user(session, telegram_user_id=911)
        await _add_feed(session, user, url="https://example.com/feed.xml")
        await session.commit()

        entry = _make_entry(url="https://example.com/article/1", title="Article")
        parsed = _make_parsed_feed([entry])

        worker = NewsWorker(
            engine=session.bind,  # type: ignore[arg-type]
            poll_interval_seconds=1,
            fetch_full_text=False,
        )

        with (
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.urllib.request.urlopen",
                return_value=_make_feed_response([]),
            ),
            patch(
                "ai_rss_news_assistant.db.rss_ingestion.feedparser.parse",
                return_value=parsed,
            ),
        ):
            # Start the worker, allow one poll cycle to complete, then stop.
            worker_task = asyncio.create_task(worker.run())
            await asyncio.sleep(0.5)
            await worker.stop()
            await worker_task

        # Worker should have processed the feed and created a news item.
        items = await session.execute(
            select(NewsItem).where(NewsItem.user_id == user.id)
        )
        db_items = items.scalars().all()
        assert len(db_items) == 1
        assert db_items[0].title == "Article"
    finally:
        await _close_session(session)
