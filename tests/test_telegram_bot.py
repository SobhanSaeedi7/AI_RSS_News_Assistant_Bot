"""Tests for the Telegram bot entry point and handlers.

Mocks Telegram and database dependencies; no real token, network, or live
database is required.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from telegram import Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import Application, ContextTypes

from ai_rss_news_assistant.bot import use_cases
from ai_rss_news_assistant.bot.handlers import (
    PENDING_FEED_KEY,
    _feed_display_name,
    add_feed_command,
    ask_command,
    build_ask_messages,
    cancel_command,
    delete_feed_command,
    delivery_command,
    help_command,
    list_feeds_command,
    mark_recipient_available_handler,
    news_command,
    pending_feed_input,
    schedule_command,
    start_command,
)
from ai_rss_news_assistant.bot.messages import (
    ADD_FEED_PROMPT,
    ASK_TOO_LONG,
    ASK_USAGE,
    CANCEL_DONE,
    CANCEL_NOTHING,
    DEL_FEED_PROMPT,
    DEL_FEED_SUCCESS_URL,
    DELIVERY_OFF,
    DELIVERY_ON,
    DELIVERY_USAGE,
    NEWS_EMPTY,
    NEWS_MAX_MESSAGE_LENGTH,
    PENDING_EXPIRED,
)
from ai_rss_news_assistant.contracts import AGENT_ERROR, AgentAnswer, AgentSource
from ai_rss_news_assistant.db.base import Base, Database
from ai_rss_news_assistant.db.models import User, UserFeed
from ai_rss_news_assistant.db.repository import (
    create_feed,
    create_news_item,
    create_user,
    get_user_by_telegram_id,
)
from ai_rss_news_assistant.feeds import FeedProbe, FeedProbeStatus

_TEST_TELEGRAM_USER_ID = 123456789
_TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
_TEST_DATABASE_URL = "postgresql+asyncpg://ai_rss_news:secret@db:5432/ai_rss_news"


def _make_update(
    telegram_user_id: int = _TEST_TELEGRAM_USER_ID,
    *,
    chat_id: int = 100,
    text: str = "",
) -> MagicMock:
    """Create a mock Update with a Telegram user and message."""
    user = MagicMock()
    user.id = telegram_user_id
    user.is_bot = False
    user.first_name = "Test"

    message = MagicMock()
    message.reply_text = AsyncMock()
    message.chat_id = chat_id
    message.text = text

    update = MagicMock(spec=Update)
    update.effective_user = user
    update.effective_message = message

    return update


def _make_context(db: MagicMock, args: list | None = None, agent=None) -> MagicMock:
    """Create a mock ContextTypes.DEFAULT_TYPE with application.bot_data."""
    application = MagicMock(spec=Application)
    application.bot_data = {"db": db}
    if agent is not None:
        application.bot_data["agent"] = agent
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.application = application
    context.args = args or []
    context.user_data = {}
    return context


def _mock_db_session(db: MagicMock) -> AsyncMock:
    """Configure db.session() to return an async context manager yielding a session."""
    mock_session = AsyncMock()
    session_manager = AsyncMock()
    session_manager.__aenter__.return_value = mock_session
    session_manager.__aexit__.return_value = None
    db.session = MagicMock(return_value=session_manager)
    return mock_session


@pytest.fixture(autouse=True)
def _stub_feed_probe(monkeypatch: pytest.MonkeyPatch):
    """Give every /addfeed test a valid probe unless it overrides this.

    The real probe performs an HTTP request; no test in this module is allowed
    to reach the network, so the default is stubbed here and the failure-path
    tests replace it explicitly.
    """

    async def _probe(url: str, timeout: int = 30) -> FeedProbe:
        return FeedProbe(status=FeedProbeStatus.VALID, title="Example Feed")

    monkeypatch.setattr("ai_rss_news_assistant.bot.use_cases.probe_feed_async", _probe)


@pytest.mark.anyio
async def test_start_command_creates_new_user() -> None:
    """Test /start creates a new user when none exists."""
    update = _make_update()
    context = _make_context(AsyncMock())

    mock_session = _mock_db_session(context.application.bot_data["db"])

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=None,
        ) as mock_get,
        patch(
            "ai_rss_news_assistant.bot.use_cases.create_user",
            return_value=MagicMock(spec=User),
        ) as mock_create,
    ):
        await start_command(update, context)

    mock_get.assert_called_once_with(mock_session, _TEST_TELEGRAM_USER_ID)
    mock_create.assert_called_once_with(mock_session, _TEST_TELEGRAM_USER_ID)
    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "سلام" in text


@pytest.mark.anyio
async def test_start_command_existing_user() -> None:
    """Test /start finds existing user without creating duplicate."""
    existing_user = MagicMock(spec=User)
    existing_user.id = "00000000-0000-0000-0000-000000000001"

    update = _make_update()
    context = _make_context(AsyncMock())

    mock_session = _mock_db_session(context.application.bot_data["db"])

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=existing_user,
        ) as mock_get,
        patch(
            "ai_rss_news_assistant.bot.use_cases.create_user",
        ) as mock_create,
    ):
        await start_command(update, context)

    mock_get.assert_called_once_with(mock_session, _TEST_TELEGRAM_USER_ID)
    mock_create.assert_not_called()
    update.effective_message.reply_text.assert_called_once()


@pytest.mark.anyio
async def test_help_command() -> None:
    """Test /help responds with help message."""
    update = _make_update()
    context = _make_context(AsyncMock())

    await help_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "/start" in text
    assert "/help" in text
    assert "/addfeed" in text
    assert "/listfeeds" in text
    assert "/delfeed" in text


@pytest.mark.anyio
async def test_add_feed_creates_new_feed() -> None:
    """Test /addfeed successfully creates a new feed."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])

    _mock_db_session(context.application.bot_data["db"])

    new_feed = MagicMock(spec=UserFeed)
    new_feed.id = uuid.UUID("00000000-0000-0000-0000-000000000100")
    new_feed.url = "https://example.com/feed.xml"
    new_feed.title = "Example Feed"

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[],
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.create_feed",
            return_value=new_feed,
        ),
    ):
        await add_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "فید با موفقیت اضافه شد" in text
    assert "https://example.com/feed.xml" in text


@pytest.mark.anyio
async def test_add_feed_missing_url() -> None:
    """Test /addfeed with no URL argument shows usage."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=[])

    await add_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "لطفاً یک آدرس فید RSS ارائه دهید" in text


@pytest.mark.anyio
async def test_add_feed_invalid_url() -> None:
    """Test /addfeed with invalid URL shows error."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["ftp://example.com/feed.xml"])

    await add_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "نامعتبر" in text


@pytest.mark.anyio
async def test_add_feed_malformed_ipv6_url() -> None:
    """Malformed IPv6 URLs are rejected before DB or network work."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["http://[::1"])

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id"
        ) as mock_get_user,
        patch("ai_rss_news_assistant.bot.use_cases.normalize_url") as mock_normalize,
    ):
        await add_feed_command(update, context)

    mock_get_user.assert_not_called()
    mock_normalize.assert_not_called()
    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "نامعتبر" in text


@pytest.mark.anyio
async def test_add_feed_missing_hostname() -> None:
    """Test /addfeed with URL missing hostname shows error."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://"])

    await add_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "نامعتبر" in text


@pytest.mark.anyio
async def test_add_feed_rejects_embedded_credentials() -> None:
    """Credentials in a feed URL must not be stored or logged."""
    update = _make_update()
    context = _make_context(
        AsyncMock(), args=["https://user:password@example.com/feed.xml"]
    )

    await add_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "نامعتبر" in text


@pytest.mark.anyio
async def test_add_feed_duplicate() -> None:
    """Test /addfeed with duplicate normalized URL shows duplicate message."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])

    _mock_db_session(context.application.bot_data["db"])

    existing_feed = MagicMock(spec=UserFeed)
    existing_feed.url = "https://example.com/feed.xml"

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[existing_feed],
        ),
    ):
        await add_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "قبلاً" in text


@pytest.mark.anyio
async def test_add_feed_integrity_error() -> None:
    """Test /addfeed with unique violation (pgcode 23505) on the feed URL constraint returns duplicate message."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])

    _mock_db_session(context.application.bot_data["db"])

    orig = MagicMock()
    orig.pgcode = "23505"
    orig.constraint_name = "uq_user_feeds_user_id_url"
    exc = IntegrityError("duplicate key violates unique constraint", None, orig)

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[],
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.create_feed",
            side_effect=exc,
        ),
    ):
        await add_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "قبلاً" in text


@pytest.mark.anyio
async def test_add_feed_unique_violation_wrong_constraint_not_duplicate() -> None:
    """Test /addfeed with pgcode 23505 but a DIFFERENT constraint name is NOT reported as duplicate."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])

    _mock_db_session(context.application.bot_data["db"])

    # Same SQLSTATE 23505 but on a different constraint (uq_user_feeds_user_id_id)
    orig = MagicMock()
    orig.pgcode = "23505"
    orig.constraint_name = "uq_user_feeds_user_id_id"
    exc = IntegrityError("duplicate key violates unique constraint", None, orig)

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[],
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.create_feed",
            side_effect=exc,
        ),
        pytest.raises(IntegrityError),
    ):
        await add_feed_command(update, context)

    # Rollback should still have been called
    db = context.application.bot_data["db"]
    assert db.session.return_value.__aenter__.return_value.rollback.called


@pytest.mark.anyio
async def test_add_feed_unique_violation_returns_duplicate() -> None:
    """Test /addfeed with PostgreSQL unique violation (pgcode 23505) returns duplicate message."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])

    _mock_db_session(context.application.bot_data["db"])

    # Simulate asyncpg UniqueViolationError with pgcode 23505
    orig = MagicMock()
    orig.pgcode = "23505"
    orig.constraint_name = "uq_user_feeds_user_id_url"
    exc = IntegrityError("duplicate key violates unique constraint", None, orig)

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[],
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.create_feed",
            side_effect=exc,
        ),
    ):
        await add_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "قبلاً" in text


@pytest.mark.anyio
async def test_add_feed_unrelated_integrity_error_not_duplicate() -> None:
    """Test /addfeed with unrelated IntegrityError (pgcode 23503) is not misreported as duplicate."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])

    _mock_db_session(context.application.bot_data["db"])

    # Simulate asyncpg ForeignKeyViolation with pgcode 23503
    orig = MagicMock()
    orig.pgcode = "23503"
    exc = IntegrityError("foreign key constraint violated", None, orig)

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[],
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.create_feed",
            side_effect=exc,
        ),
        pytest.raises(IntegrityError),
    ):
        await add_feed_command(update, context)

    # Session rollback should have been called to reset the aborted transaction
    db = context.application.bot_data["db"]
    assert db.session.return_value.__aenter__.return_value.rollback.called


@pytest.mark.anyio
async def test_add_feed_session_rolled_back_after_integrity_error() -> None:
    """Test that IntegrityError triggers session.rollback() so commit is safe."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])

    _mock_db_session(context.application.bot_data["db"])
    mock_session = context.application.bot_data[
        "db"
    ].session.return_value.__aenter__.return_value

    orig = MagicMock()
    orig.pgcode = "23505"
    orig.constraint_name = "uq_user_feeds_user_id_url"
    exc = IntegrityError("duplicate key violates unique constraint", None, orig)

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[],
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.create_feed",
            side_effect=exc,
        ),
    ):
        await add_feed_command(update, context)

    mock_session.rollback.assert_called_once()


@pytest.mark.anyio
async def test_add_feed_concurrent_safety_via_unique_constraint() -> None:
    """Test that concurrent duplicate creation is prevented by DB unique constraint."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])

    _mock_db_session(context.application.bot_data["db"])

    # list_feeds returns empty (first request wins the check)
    # create_feed raises IntegrityError (second request's insert conflicts)
    orig = MagicMock()
    orig.pgcode = "23505"
    orig.constraint_name = "uq_user_feeds_user_id_url"
    exc = IntegrityError("duplicate key violates unique constraint", None, orig)

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[],
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.create_feed",
            side_effect=exc,
        ),
    ):
        await add_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "قبلاً" in text


@pytest.mark.anyio
async def test_add_feed_unreachable_url_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL that cannot be fetched must not be stored as a subscription."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])
    _mock_db_session(context.application.bot_data["db"])

    async def _unreachable(url: str, timeout: int = 30) -> FeedProbe:
        return FeedProbe(status=FeedProbeStatus.UNREACHABLE)

    monkeypatch.setattr(
        "ai_rss_news_assistant.bot.use_cases.probe_feed_async", _unreachable
    )

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch("ai_rss_news_assistant.bot.use_cases.list_feeds", return_value=[]),
        patch("ai_rss_news_assistant.bot.use_cases.create_feed") as mock_create,
    ):
        await add_feed_command(update, context)

    mock_create.assert_not_called()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "در دسترس نیست" in text


@pytest.mark.anyio
async def test_add_feed_html_page_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTML page is not a feed, even though the URL is well formed."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/"])
    _mock_db_session(context.application.bot_data["db"])

    async def _not_a_feed(url: str, timeout: int = 30) -> FeedProbe:
        return FeedProbe(status=FeedProbeStatus.NOT_A_FEED)

    monkeypatch.setattr(
        "ai_rss_news_assistant.bot.use_cases.probe_feed_async", _not_a_feed
    )

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch("ai_rss_news_assistant.bot.use_cases.list_feeds", return_value=[]),
        patch("ai_rss_news_assistant.bot.use_cases.create_feed") as mock_create,
    ):
        await add_feed_command(update, context)

    mock_create.assert_not_called()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "فید RSS/Atom معتبر نیست" in text


@pytest.mark.anyio
async def test_add_feed_blocked_destination_is_rejected() -> None:
    """A private destination is refused before any request is made."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["http://127.0.0.1/feed.xml"])
    _mock_db_session(context.application.bot_data["db"])

    # The autouse stub would accept anything; patch the real probe's seam to
    # prove the URL itself is refused here without touching the network.
    with (
        patch("ai_rss_news_assistant.bot.use_cases.probe_feed_async") as mock_probe,
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch("ai_rss_news_assistant.bot.use_cases.list_feeds", return_value=[]),
        patch("ai_rss_news_assistant.bot.use_cases.create_feed") as mock_create,
    ):
        from ai_rss_news_assistant.feeds import probe_feed_async as real_probe

        mock_probe.side_effect = real_probe
        await add_feed_command(update, context)

    mock_create.assert_not_called()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "مجاز نیست" in text


@pytest.mark.anyio
async def test_add_feed_stores_the_feed_title() -> None:
    """The title from the feed itself is persisted, so /listfeeds can show it."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])
    _mock_db_session(context.application.bot_data["db"])

    created = MagicMock(spec=UserFeed)
    created.url = "https://example.com/feed.xml"
    created.title = "Example Feed"

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch("ai_rss_news_assistant.bot.use_cases.list_feeds", return_value=[]),
        patch(
            "ai_rss_news_assistant.bot.use_cases.create_feed", return_value=created
        ) as mock_create,
    ):
        await add_feed_command(update, context)

    assert mock_create.call_args.kwargs["title"] == "Example Feed"
    text = update.effective_message.reply_text.call_args[0][0]
    assert "Example Feed" in text


@pytest.mark.anyio
async def test_add_feed_empty_feed_is_accepted_with_a_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid feed with no items yet is stored, with an honest message."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])
    _mock_db_session(context.application.bot_data["db"])

    async def _empty(url: str, timeout: int = 30) -> FeedProbe:
        return FeedProbe(status=FeedProbeStatus.EMPTY, title="New Feed")

    monkeypatch.setattr("ai_rss_news_assistant.bot.use_cases.probe_feed_async", _empty)

    created = MagicMock(spec=UserFeed)
    created.url = "https://example.com/feed.xml"
    created.title = "New Feed"

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch("ai_rss_news_assistant.bot.use_cases.list_feeds", return_value=[]),
        patch("ai_rss_news_assistant.bot.use_cases.create_feed", return_value=created),
    ):
        await add_feed_command(update, context)

    text = update.effective_message.reply_text.call_args[0][0]
    assert "هیچ خبری ندارد" in text


@pytest.mark.anyio
async def test_add_feed_duplicate_does_not_probe_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-adding an existing feed must not cost a network request."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["https://example.com/feed.xml"])
    _mock_db_session(context.application.bot_data["db"])

    existing = MagicMock(spec=UserFeed)
    existing.url = "https://example.com/feed.xml"

    calls: list[str] = []

    async def _probe(url: str, timeout: int = 30) -> FeedProbe:
        calls.append(url)
        return FeedProbe(status=FeedProbeStatus.VALID)

    monkeypatch.setattr("ai_rss_news_assistant.bot.use_cases.probe_feed_async", _probe)

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[existing],
        ),
    ):
        await add_feed_command(update, context)

    assert calls == []
    text = update.effective_message.reply_text.call_args[0][0]
    assert "قبلاً" in text


@pytest.mark.anyio
async def test_list_feeds_shows_a_failing_feed() -> None:
    """A feed in backoff is visible in /listfeeds instead of looking healthy."""
    update = _make_update()
    context = _make_context(AsyncMock())
    _mock_db_session(context.application.bot_data["db"])

    feed = MagicMock(spec=UserFeed)
    feed.id = uuid.UUID("00000000-0000-0000-0000-000000000100")
    feed.title = "Broken Feed"
    feed.url = "https://example.com/feed.xml"
    feed.active = True
    feed.last_error = "unreachable"

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch("ai_rss_news_assistant.bot.use_cases.list_feeds", return_value=[feed]),
    ):
        await list_feeds_command(update, context)

    text = update.effective_message.reply_text.call_args[0][0]
    assert "Broken Feed" in text
    assert "در دسترس نیست" in text


@pytest.mark.anyio
async def test_list_feeds_empty() -> None:
    """Test /listfeeds with no feeds shows empty message."""
    update = _make_update()
    context = _make_context(AsyncMock())

    _mock_db_session(context.application.bot_data["db"])

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[],
        ),
    ):
        await list_feeds_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "هیچ فیدی ثبت نشده است" in text


@pytest.mark.anyio
async def test_list_feeds_with_feeds() -> None:
    """Test /listfeeds lists user's feeds."""
    update = _make_update()
    context = _make_context(AsyncMock())

    _mock_db_session(context.application.bot_data["db"])

    feed1 = MagicMock(spec=UserFeed)
    feed1.id = uuid.UUID("00000000-0000-0000-0000-000000000100")
    feed1.title = "Example Feed"
    feed1.url = "https://example.com/feed.xml"
    feed1.active = True

    feed2 = MagicMock(spec=UserFeed)
    feed2.id = uuid.UUID("00000000-0000-0000-0000-000000000200")
    feed2.title = None
    feed2.url = "https://other.com/feed.xml"
    feed2.active = False

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[feed1, feed2],
        ),
    ):
        await list_feeds_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "Example Feed" in text
    assert "https://example.com/feed.xml" in text
    assert "فعال" in text
    assert "غیرفعال" in text


@pytest.mark.anyio
async def test_list_feeds_splits_long_output() -> None:
    """A user with many/long feeds does not get one over-limit message."""
    update = _make_update()
    context = _make_context(AsyncMock())

    _mock_db_session(context.application.bot_data["db"])

    feed = MagicMock(spec=UserFeed)
    feed.id = uuid.UUID("00000000-0000-0000-0000-000000000100")
    feed.title = "T" * 2000
    feed.url = "https://example.com/" + "a" * 2000
    feed.active = True

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.list_feeds",
            return_value=[feed, feed, feed],
        ),
    ):
        await list_feeds_command(update, context)

    assert update.effective_message.reply_text.call_count > 1
    for call in update.effective_message.reply_text.call_args_list:
        assert len(call.args[0]) <= 4000


@pytest.mark.anyio
async def test_delete_feed_success() -> None:
    """Test /delfeed successfully deletes an owned feed."""
    feed_id = uuid.UUID("00000000-0000-0000-0000-000000000100")
    update = _make_update()
    context = _make_context(AsyncMock(), args=[str(feed_id)])

    _mock_db_session(context.application.bot_data["db"])

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.delete_feed",
            return_value=True,
        ),
    ):
        await delete_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "حذف شد" in text
    assert str(feed_id) in text


@pytest.mark.anyio
async def test_delete_feed_not_found() -> None:
    """Test /delfeed with non-existent or non-owned feed ID."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["00000000-0000-0000-0000-000000000999"])

    _mock_db_session(context.application.bot_data["db"])

    with (
        patch(
            "ai_rss_news_assistant.bot.use_cases.get_user_by_telegram_id",
            return_value=MagicMock(spec=User),
        ),
        patch(
            "ai_rss_news_assistant.bot.use_cases.delete_feed",
            return_value=False,
        ),
    ):
        await delete_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "یافت نشد" in text or "دسترسی به آن ندارید" in text


@pytest.mark.anyio
async def test_delete_feed_invalid_id() -> None:
    """Test /delfeed with invalid UUID format."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["not-a-uuid"])

    await delete_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "یافت نشد" in text or "دسترسی به آن ندارید" in text


@pytest.mark.anyio
async def test_delete_feed_missing_id_starts_the_two_step_flow() -> None:
    """Test /delfeed with no argument asks for the identifier."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=[])

    await delete_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert text == DEL_FEED_PROMPT
    assert PENDING_FEED_KEY in context.user_data


def test_error_handler_does_not_leak_secrets(caplog) -> None:
    """Test _error_handler logs the error without exposing secrets."""
    context = MagicMock()
    context.error = Exception("database connection failed")

    with caplog.at_level(logging.ERROR, logger="ai_rss_news_assistant.bot.handlers"):
        from ai_rss_news_assistant.bot.handlers import _error_handler

        asyncio.run(_error_handler(None, context))

    assert "database connection failed" in caplog.text
    assert _TEST_BOT_TOKEN not in caplog.text
    assert "secret" not in caplog.text.lower()


def test_main_disposes_db_on_application_build_failure() -> None:
    """Database engine is disposed if Application construction fails."""
    mock_db = MagicMock()
    mock_db.dispose = AsyncMock()
    mock_loop = MagicMock()

    with (
        patch("ai_rss_news_assistant.bot.entrypoint.Database", return_value=mock_db),
        patch(
            "ai_rss_news_assistant.bot.entrypoint.ApplicationBuilder"
        ) as mock_builder,
        patch(
            "ai_rss_news_assistant.bot.entrypoint.asyncio.new_event_loop",
            return_value=mock_loop,
        ),
        patch("ai_rss_news_assistant.bot.entrypoint.asyncio.set_event_loop"),
        patch.dict(
            "os.environ",
            {"BOT_TOKEN": _TEST_BOT_TOKEN, "DATABASE_URL": _TEST_DATABASE_URL},
            clear=True,
        ),
    ):
        mock_builder.return_value.token.return_value.build.side_effect = RuntimeError(
            "build failed"
        )

        with pytest.raises(RuntimeError, match="build failed"):
            from ai_rss_news_assistant.bot.entrypoint import run_bot

            run_bot()

        dispose_coroutines = [
            call[0][0]
            for call in mock_loop.run_until_complete.call_args_list
            if asyncio.iscoroutine(call[0][0])
        ]
        assert len(dispose_coroutines) == 1
        mock_loop.close.assert_called_once()


def test_main_disposes_db_on_polling_failure() -> None:
    """Database engine is disposed if polling raises an exception."""
    mock_db = MagicMock()
    mock_db.dispose = AsyncMock()
    mock_loop = MagicMock()
    mock_application = MagicMock()
    mock_application.updater.running = False
    mock_application.running = False

    with (
        patch("ai_rss_news_assistant.bot.entrypoint.Database", return_value=mock_db),
        patch(
            "ai_rss_news_assistant.bot.entrypoint.ApplicationBuilder"
        ) as mock_builder,
        patch(
            "ai_rss_news_assistant.bot.entrypoint.asyncio.new_event_loop",
            return_value=mock_loop,
        ),
        patch("ai_rss_news_assistant.bot.entrypoint.asyncio.set_event_loop"),
        patch.dict(
            "os.environ",
            {"BOT_TOKEN": _TEST_BOT_TOKEN, "DATABASE_URL": _TEST_DATABASE_URL},
            clear=True,
        ),
    ):
        mock_builder.return_value.token.return_value.build.return_value = (
            mock_application
        )
        mock_loop.run_forever.side_effect = RuntimeError("polling failed")

        with pytest.raises(RuntimeError, match="polling failed"):
            from ai_rss_news_assistant.bot.entrypoint import run_bot

            run_bot()

        dispose_coroutines = [
            call[0][0]
            for call in mock_loop.run_until_complete.call_args_list
            if asyncio.iscoroutine(call[0][0])
        ]
        assert len(dispose_coroutines) == 1
        mock_loop.close.assert_called_once()


def test_main_disposes_db_exactly_once_on_normal_shutdown() -> None:
    """Database engine is disposed exactly once on normal shutdown."""
    mock_db = MagicMock()
    mock_db.dispose = AsyncMock()
    mock_loop = MagicMock()
    mock_application = MagicMock()
    mock_application.updater.running = False
    mock_application.running = False

    with (
        patch("ai_rss_news_assistant.bot.entrypoint.Database", return_value=mock_db),
        patch(
            "ai_rss_news_assistant.bot.entrypoint.ApplicationBuilder"
        ) as mock_builder,
        patch(
            "ai_rss_news_assistant.bot.entrypoint.asyncio.new_event_loop",
            return_value=mock_loop,
        ),
        patch("ai_rss_news_assistant.bot.entrypoint.asyncio.set_event_loop"),
        patch.dict(
            "os.environ",
            {"BOT_TOKEN": _TEST_BOT_TOKEN, "DATABASE_URL": _TEST_DATABASE_URL},
            clear=True,
        ),
    ):
        mock_builder.return_value.token.return_value.build.return_value = (
            mock_application
        )
        mock_loop.run_forever.side_effect = KeyboardInterrupt()

        from ai_rss_news_assistant.bot.entrypoint import run_bot

        run_bot()

        dispose_coroutines = [
            call[0][0]
            for call in mock_loop.run_until_complete.call_args_list
            if asyncio.iscoroutine(call[0][0])
        ]
        assert len(dispose_coroutines) == 1
        mock_loop.close.assert_called_once()


def test_main_handles_invalid_token_without_leaking_token(caplog) -> None:
    """InvalidToken is logged safely and the database is still disposed."""
    mock_db = MagicMock()
    mock_db.dispose = MagicMock(return_value=None)
    mock_loop = MagicMock()
    mock_application = MagicMock()
    mock_application.initialize.return_value = None
    mock_application.updater.running = False
    mock_application.running = False

    calls = 0

    def run_until_complete(coro):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InvalidToken(f"Invalid token: {_TEST_BOT_TOKEN}")

    mock_loop.run_until_complete.side_effect = run_until_complete

    with (
        patch("ai_rss_news_assistant.bot.entrypoint.Database", return_value=mock_db),
        patch(
            "ai_rss_news_assistant.bot.entrypoint.ApplicationBuilder"
        ) as mock_builder,
        patch("ai_rss_news_assistant.bot.entrypoint.setup_logging"),
        patch(
            "ai_rss_news_assistant.bot.entrypoint.asyncio.new_event_loop",
            return_value=mock_loop,
        ),
        patch("ai_rss_news_assistant.bot.entrypoint.asyncio.set_event_loop"),
        patch.dict(
            "os.environ",
            {"BOT_TOKEN": _TEST_BOT_TOKEN, "DATABASE_URL": _TEST_DATABASE_URL},
            clear=True,
        ),
        caplog.at_level(logging.ERROR, logger="ai_rss_news_assistant.bot.entrypoint"),
    ):
        mock_builder.return_value.token.return_value.build.return_value = (
            mock_application
        )

        from ai_rss_news_assistant.bot.entrypoint import run_bot

        run_bot()

    assert "rejected BOT_TOKEN" in caplog.text
    assert _TEST_BOT_TOKEN not in caplog.text
    mock_loop.close.assert_called_once()


# --- /news command (real database) ---


async def _make_real_db(tmp_path) -> Database:
    """Create a file-backed SQLite database with the application schema."""
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'telegram-news.db').as_posix()}")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return db


async def _seed_news(
    db: Database,
    telegram_user_id: int,
    entries: list[dict],
    feed_title: str | None = "Example Feed",
) -> uuid.UUID:
    """Create a user, a feed, and the supplied news entries."""
    async with db.session() as session:
        user = await create_user(session, telegram_user_id)
        feed = await create_feed(
            session,
            user.id,
            f"https://example.com/{telegram_user_id}/feed.xml",
            title=feed_title,
        )
        for index, entry in enumerate(entries):
            await create_news_item(
                session=session,
                user_id=user.id,
                feed_id=feed.id,
                url=entry.get("url", f"https://example.com/{telegram_user_id}/{index}"),
                title=entry["title"],
                published_at=entry.get("published_at"),
                fetched_at=entry.get(
                    "fetched_at",
                    datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
                ),
            )
        return user.id


@pytest.mark.anyio
async def test_news_command_returns_latest_five_own_items(tmp_path) -> None:
    """Only the five most recent articles are sent, in newest-first order."""
    db = await _make_real_db(tmp_path)
    try:
        entries = [
            {
                "title": f"Article {index}",
                "published_at": datetime(2026, 1, index, tzinfo=UTC),
            }
            for index in range(1, 7)
        ]
        await _seed_news(db, _TEST_TELEGRAM_USER_ID, entries)

        update = _make_update()
        context = _make_context(db)
        await news_command(update, context)

        calls = update.effective_message.reply_text.await_args_list
        assert len(calls) == 1
        text = calls[0].args[0]
        assert calls[0].kwargs["parse_mode"] == "HTML"
        assert "Article 6" in text
        assert "Article 2" in text
        assert "Article 1" not in text
        assert text.count("لینک:") == 5
        assert "منبع: Example Feed" in text
        assert "تاریخ انتشار: 2026-01-06" in text
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_news_command_empty_state(tmp_path) -> None:
    """A user without stored news gets the friendly Persian message."""
    db = await _make_real_db(tmp_path)
    try:
        update = _make_update()
        context = _make_context(db)
        await news_command(update, context)

        update.effective_message.reply_text.assert_awaited_once_with(NEWS_EMPTY)
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_news_command_existing_user_without_news(tmp_path) -> None:
    """An existing account with no articles gets the Persian empty state."""
    db = await _make_real_db(tmp_path)
    try:
        await _seed_news(db, _TEST_TELEGRAM_USER_ID, [])

        # Precondition: the user record exists, so only the news is missing.
        async with db.session() as session:
            user = await get_user_by_telegram_id(session, _TEST_TELEGRAM_USER_ID)
        assert user is not None

        update = _make_update()
        context = _make_context(db)
        await news_command(update, context)

        update.effective_message.reply_text.assert_awaited_once_with(NEWS_EMPTY)
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_news_command_falls_back_to_feed_hostname_without_title(tmp_path) -> None:
    """A feed without a title is shown by hostname in well-formed HTML."""
    db = await _make_real_db(tmp_path)
    try:
        await _seed_news(
            db,
            _TEST_TELEGRAM_USER_ID,
            [{"title": "Fallback article"}],
            feed_title=None,
        )

        update = _make_update()
        context = _make_context(db)
        await news_command(update, context)

        calls = update.effective_message.reply_text.await_args_list
        assert len(calls) == 1
        text = calls[0].args[0]
        assert calls[0].kwargs["parse_mode"] == "HTML"
        assert "Fallback article" in text
        assert "منبع: example.com" in text
        assert "None" not in text
        assert text.count("<b>") == text.count("</b>") == 1
    finally:
        await db.dispose()


def test_feed_display_name_falls_back_for_malformed_feed_url() -> None:
    """A malformed stored feed URL is shown raw instead of raising."""
    feed = UserFeed(user_id=uuid.uuid4(), url="http://[::1", title=None)

    assert _feed_display_name(feed) == "http://[::1"


@pytest.mark.anyio
async def test_news_command_does_not_leak_other_users_news(tmp_path) -> None:
    """User A only receives articles belonging to user A."""
    db = await _make_real_db(tmp_path)
    try:
        await _seed_news(
            db,
            _TEST_TELEGRAM_USER_ID,
            [{"title": "A private article"}],
            feed_title="A Feed",
        )
        await _seed_news(
            db,
            987654321,
            [{"title": "B private article"}],
            feed_title="B Feed",
        )

        update = _make_update()
        context = _make_context(db)
        await news_command(update, context)

        calls = update.effective_message.reply_text.await_args_list
        assert len(calls) == 1
        text = calls[0].args[0]
        assert "A private article" in text
        assert "منبع: A Feed" in text
        assert "B private article" not in text
        assert "B Feed" not in text
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_news_command_escapes_untrusted_feed_text(tmp_path) -> None:
    """Feed titles and feed names are HTML-escaped before sending."""
    db = await _make_real_db(tmp_path)
    try:
        await _seed_news(
            db,
            _TEST_TELEGRAM_USER_ID,
            [
                {
                    "title": "<b>Breaking</b> & <script>alert(1)</script>",
                }
            ],
            feed_title='<i>Feed "x"</i>',
        )

        update = _make_update()
        context = _make_context(db)
        await news_command(update, context)

        text = update.effective_message.reply_text.await_args.args[0]
        assert (
            "&lt;b&gt;Breaking&lt;/b&gt; &amp; &lt;script&gt;alert(1)&lt;/script&gt;"
            in text
        )
        assert "&lt;i&gt;Feed &quot;x&quot;&lt;/i&gt;" not in text
        assert '&lt;i&gt;Feed "x"&lt;/i&gt;' in text
        assert "<script>" not in text
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_news_command_handles_missing_dates_and_long_output(tmp_path) -> None:
    """Missing dates are omitted and long articles are split under the limit."""
    db = await _make_real_db(tmp_path)
    try:
        long_title = "T" * 2000
        long_suffix = "a" * 2000
        entries = [
            {
                "title": long_title,
                "url": f"https://example.com/{index}/{long_suffix}",
                "published_at": None,
            }
            for index in range(5)
        ]
        await _seed_news(db, _TEST_TELEGRAM_USER_ID, entries)

        update = _make_update()
        context = _make_context(db)
        await news_command(update, context)

        calls = update.effective_message.reply_text.await_args_list
        assert len(calls) >= 2
        texts = [call.args[0] for call in calls]
        assert all(len(text) <= NEWS_MAX_MESSAGE_LENGTH for text in texts)
        combined = "".join(texts)
        assert "تاریخ انتشار" not in combined
        assert "…" in combined
        assert all(call.kwargs["parse_mode"] == "HTML" for call in calls)
    finally:
        await db.dispose()


# --- /delivery command (real database) ---


async def _delivery_preference(db: Database, telegram_user_id: int) -> bool:
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user_id)
        assert user is not None
        return user.auto_delivery_enabled


@pytest.mark.anyio
async def test_delivery_command_toggles_off_then_on(tmp_path) -> None:
    """`/delivery` flips the persisted switch on every invocation."""
    db = await _make_real_db(tmp_path)
    try:
        update = _make_update()
        await delivery_command(update, _make_context(db))

        update.effective_message.reply_text.assert_awaited_once_with(DELIVERY_OFF)
        assert await _delivery_preference(db, _TEST_TELEGRAM_USER_ID) is False

        second = _make_update()
        await delivery_command(second, _make_context(db))

        second.effective_message.reply_text.assert_awaited_once_with(DELIVERY_ON)
        assert await _delivery_preference(db, _TEST_TELEGRAM_USER_ID) is True
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_delivery_command_turns_on_when_disabled(tmp_path) -> None:
    """A user who disabled delivery is re-enabled by the no-argument toggle."""
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            await create_user(
                session, _TEST_TELEGRAM_USER_ID, auto_delivery_enabled=False
            )

        update = _make_update()
        await delivery_command(update, _make_context(db))

        update.effective_message.reply_text.assert_awaited_once_with(DELIVERY_ON)
        assert await _delivery_preference(db, _TEST_TELEGRAM_USER_ID) is True
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_delivery_command_rejects_arguments(tmp_path) -> None:
    """Arguments are not a preference form any more; usage is shown instead."""
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            await create_user(session, _TEST_TELEGRAM_USER_ID)

        for args in (["off"], ["on"], ["maybe"], ["off", "extra"]):
            update = _make_update()
            await delivery_command(update, _make_context(db, args=args))
            update.effective_message.reply_text.assert_awaited_once_with(DELIVERY_USAGE)

        assert await _delivery_preference(db, _TEST_TELEGRAM_USER_ID) is True
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_delivery_preference_is_per_user(tmp_path) -> None:
    """Toggling delivery for one user does not affect another user."""
    db = await _make_real_db(tmp_path)
    try:
        other_telegram_id = _TEST_TELEGRAM_USER_ID + 1
        async with db.session() as session:
            await create_user(session, other_telegram_id)

        await delivery_command(_make_update(), _make_context(db))

        assert await _delivery_preference(db, _TEST_TELEGRAM_USER_ID) is False
        assert await _delivery_preference(db, other_telegram_id) is True
    finally:
        await db.dispose()


# --- /ask command ---


class _FakeAgent:
    """Stand-in for NewsAgent that records calls and returns a canned answer."""

    def __init__(self, answer: AgentAnswer | None = None, error=None) -> None:
        self.calls: list[tuple] = []
        self._answer = answer
        self._error = error

    async def answer(self, user_id, question: str) -> AgentAnswer:
        self.calls.append((user_id, question))
        if self._error is not None:
            raise self._error
        return self._answer


def _grounded_answer(
    text: str = "پاسخ آزمایشی [1]",
    url: str = "https://example.com/ai",
) -> AgentAnswer:
    return AgentAnswer(
        text=text,
        sources=[AgentSource(title="هوش مصنوعی در ایران", url=url)],
        grounded=True,
    )


@pytest.mark.anyio
async def test_schedule_command_shows_the_default_interval() -> None:
    """A new user is shown the default cadence, not an error."""
    update = _make_update()
    context = _make_context(AsyncMock())
    _mock_db_session(context.application.bot_data["db"])

    with patch(
        "ai_rss_news_assistant.bot.use_cases.delivery_interval",
        return_value=0,
    ):
        await schedule_command(update, context)

    text = update.effective_message.reply_text.call_args[0][0]
    assert "فوری" in text
    assert "/schedule" in text


@pytest.mark.anyio
async def test_schedule_command_sets_an_interval() -> None:
    """`/schedule 6h` stores 21600 seconds for the calling user."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["6h"])
    db = context.application.bot_data["db"]
    _mock_db_session(db)

    with patch(
        "ai_rss_news_assistant.bot.use_cases.set_delivery_interval",
        new_callable=AsyncMock,
        return_value=21600,
    ) as mock_set:
        await schedule_command(update, context)

    mock_set.assert_awaited_once_with(db, _make_update().effective_user.id, 21600)
    text = update.effective_message.reply_text.call_args[0][0]
    assert "6 ساعت" in text


@pytest.mark.anyio
async def test_schedule_command_shows_a_stored_interval() -> None:
    update = _make_update()
    context = _make_context(AsyncMock())
    _mock_db_session(context.application.bot_data["db"])

    with patch(
        "ai_rss_news_assistant.bot.use_cases.delivery_interval",
        return_value=86400,
    ):
        await schedule_command(update, context)

    text = update.effective_message.reply_text.call_args[0][0]
    assert "24 ساعت" in text


@pytest.mark.anyio
@pytest.mark.parametrize("raw", ["5m", "90m", "weekly", "3600"])
async def test_schedule_command_rejects_unsupported_intervals(raw: str) -> None:
    """Only the documented presets are accepted; nothing is written."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=[raw])
    _mock_db_session(context.application.bot_data["db"])

    with patch("ai_rss_news_assistant.bot.use_cases.set_delivery_interval") as mock_set:
        await schedule_command(update, context)

    mock_set.assert_not_called()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "بازه‌های مجاز" in text


@pytest.mark.anyio
async def test_schedule_command_rejects_extra_arguments() -> None:
    update = _make_update()
    context = _make_context(AsyncMock(), args=["1h", "6h"])
    _mock_db_session(context.application.bot_data["db"])

    with patch("ai_rss_news_assistant.bot.use_cases.set_delivery_interval") as mock_set:
        await schedule_command(update, context)

    mock_set.assert_not_called()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "بازه‌های مجاز" in text


@pytest.mark.anyio
async def test_schedule_command_accepts_the_immediate_alias() -> None:
    update = _make_update()
    context = _make_context(AsyncMock(), args=["فوری"])
    db = context.application.bot_data["db"]
    _mock_db_session(db)

    with patch(
        "ai_rss_news_assistant.bot.use_cases.set_delivery_interval",
        new_callable=AsyncMock,
        return_value=0,
    ) as mock_set:
        await schedule_command(update, context)

    mock_set.assert_awaited_once_with(db, _make_update().effective_user.id, 0)


@pytest.mark.anyio
async def test_help_lists_the_schedule_command() -> None:
    update = _make_update()
    context = _make_context(AsyncMock())

    await help_command(update, context)

    text = update.effective_message.reply_text.call_args[0][0]
    assert "/schedule" in text


@pytest.mark.anyio
async def test_ask_command_requires_a_question() -> None:
    agent = _FakeAgent(_grounded_answer())
    update = _make_update()

    await ask_command(update, _make_context(AsyncMock(), agent=agent))

    update.effective_message.reply_text.assert_awaited_once_with(ASK_USAGE)
    assert agent.calls == []


@pytest.mark.anyio
async def test_ask_command_rejects_long_question() -> None:
    agent = _FakeAgent(_grounded_answer())
    update = _make_update()

    await ask_command(
        update,
        _make_context(AsyncMock(), args=["x" * 501], agent=agent),
    )

    update.effective_message.reply_text.assert_awaited_once_with(ASK_TOO_LONG)
    assert agent.calls == []


@pytest.mark.anyio
async def test_ask_command_renders_answer_and_sources(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        agent = _FakeAgent(_grounded_answer())
        update = _make_update()

        await ask_command(
            update,
            _make_context(db, args=["خبرهای", "فناوری"], agent=agent),
        )

        calls = update.effective_message.reply_text.await_args_list
        assert len(calls) == 1
        text = calls[0].args[0]
        assert "پاسخ آزمایشی" in text
        assert '<a href="https://example.com/ai">' in text
        assert "هوش مصنوعی در ایران" in text
        assert calls[0].kwargs["parse_mode"] == "HTML"
        assert calls[0].kwargs["disable_web_page_preview"] is True
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_ask_command_passes_internal_user_id(tmp_path) -> None:
    """The handler resolves the Telegram user to its internal id for retrieval."""
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            user = await create_user(session, _TEST_TELEGRAM_USER_ID)
            expected_id = user.id

        agent = _FakeAgent(_grounded_answer())
        update = _make_update()

        await ask_command(update, _make_context(db, args=["سؤال"], agent=agent))

        assert agent.calls == [(expected_id, "سؤال")]
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_ask_command_splits_long_answers(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        agent = _FakeAgent(AgentAnswer(text="کلمه " * 2000))
        update = _make_update()

        await ask_command(update, _make_context(db, args=["سؤال"], agent=agent))

        calls = update.effective_message.reply_text.await_args_list
        assert len(calls) >= 2
        assert all(len(call.args[0]) <= 4000 for call in calls)
        assert all(call.kwargs["parse_mode"] == "HTML" for call in calls)
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_ask_command_handles_agent_failure(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        agent = _FakeAgent(error=RuntimeError("boom"))
        update = _make_update()

        await ask_command(update, _make_context(db, args=["سؤال"], agent=agent))

        calls = update.effective_message.reply_text.await_args_list
        assert len(calls) == 1
        assert calls[0].args[0] == AGENT_ERROR
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_ask_command_survives_telegram_error(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        agent = _FakeAgent(_grounded_answer())
        update = _make_update()
        update.effective_message.reply_text.side_effect = TelegramError("boom")

        await ask_command(update, _make_context(db, args=["سؤال"], agent=agent))

        update.effective_message.reply_text.assert_awaited_once()
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_ask_command_escapes_html_in_answer(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        agent = _FakeAgent(AgentAnswer(text="<script>alert(1)</script> [1]"))
        update = _make_update()

        await ask_command(update, _make_context(db, args=["سؤال"], agent=agent))

        text = update.effective_message.reply_text.await_args.args[0]
        assert "&lt;script&gt;" in text
        assert "<script>" not in text
    finally:
        await db.dispose()


def test_build_ask_messages_omits_sources_when_not_grounded() -> None:
    messages = build_ask_messages(AgentAnswer(text="اطلاعات کافی پیدا نشد."))

    assert messages == ["اطلاعات کافی پیدا نشد."]


def test_split_message_never_breaks_html_entities() -> None:
    from ai_rss_news_assistant.bot.handlers import _split_message

    word = "&amp;" * 100
    chunks = _split_message(word, 17)

    assert "".join(chunks) == word
    assert all(len(chunk) <= 17 for chunk in chunks)
    assert all(len(chunk) % 5 == 0 for chunk in chunks)


def test_build_ask_messages_does_not_link_unsafe_urls() -> None:
    messages = build_ask_messages(
        AgentAnswer(
            text="پاسخ [1]",
            sources=[AgentSource(title="<b>bad</b>", url="javascript:alert(1)")],
            grounded=True,
        )
    )

    assert "<a href=" not in messages[0]
    assert "&lt;b&gt;bad&lt;/b&gt;" in messages[0]


def test_build_ask_messages_escapes_source_url_attributes() -> None:
    messages = build_ask_messages(
        AgentAnswer(
            text="پاسخ [1]",
            sources=[AgentSource(title="t", url='https://example.com/"><b>')],
            grounded=True,
        )
    )

    assert 'href="https://example.com/&quot;&gt;&lt;b&gt;"' in messages[0]


def test_build_ask_messages_keeps_the_original_citation_numbers() -> None:
    """A source cited as [2] is labelled [2], not renumbered to its list position."""
    messages = build_ask_messages(
        AgentAnswer(
            text="پاسخ [2]",
            sources=[
                AgentSource(
                    title="منبع دوم",
                    url="https://example.com/two",
                    citation_index=2,
                )
            ],
            grounded=True,
        )
    )

    text = "\n".join(messages)
    assert "[2]" in text
    assert "[1]" not in text


def test_build_ask_messages_numbers_uncited_sources_by_position() -> None:
    """A source without a citation_index still renders as [1] for compatibility."""
    messages = build_ask_messages(
        AgentAnswer(
            text="پاسخ",
            sources=[AgentSource(title="منبع اول", url=None)],
            grounded=True,
        )
    )

    assert "[1] منبع اول" in messages[0]


# --- two-step /addfeed and /delfeed ---------------------------------------


@pytest.mark.anyio
async def test_add_feed_two_step_creates_the_feed(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        update = _make_update()
        context = _make_context(db, args=[])
        await add_feed_command(update, context)

        update.effective_message.reply_text.assert_awaited_once_with(ADD_FEED_PROMPT)
        assert context.user_data[PENDING_FEED_KEY]["action"] == "add"

        follow_up = _make_update(text="https://example.com/feed.xml")
        await pending_feed_input(follow_up, context)

        assert PENDING_FEED_KEY not in context.user_data
        feeds = await use_cases.list_user_feeds(db, _TEST_TELEGRAM_USER_ID)
        assert [feed.url for feed in feeds] == ["https://example.com/feed.xml"]
        assert (
            "فید با موفقیت اضافه شد"
            in (follow_up.effective_message.reply_text.await_args.args[0])
        )
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_add_feed_two_step_can_be_cancelled(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        context = _make_context(db, args=[])
        await add_feed_command(_make_update(), context)

        cancel_update = _make_update()
        await cancel_command(cancel_update, context)

        cancel_update.effective_message.reply_text.assert_awaited_once_with(CANCEL_DONE)
        assert PENDING_FEED_KEY not in context.user_data

        follow_up = _make_update(text="https://example.com/feed.xml")
        await pending_feed_input(follow_up, context)
        follow_up.effective_message.reply_text.assert_not_awaited()
        assert await use_cases.list_user_feeds(db, _TEST_TELEGRAM_USER_ID) == []
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_cancel_without_a_pending_request_is_harmless() -> None:
    update = _make_update()
    context = _make_context(AsyncMock())

    await cancel_command(update, context)

    update.effective_message.reply_text.assert_awaited_once_with(CANCEL_NOTHING)


@pytest.mark.anyio
async def test_pending_input_expires() -> None:
    context = _make_context(AsyncMock())
    context.user_data[PENDING_FEED_KEY] = {
        "action": "add",
        "telegram_user_id": _TEST_TELEGRAM_USER_ID,
        "chat_id": 100,
        "expires_at": datetime.now(UTC) - timedelta(seconds=1),
    }

    follow_up = _make_update(text="https://example.com/feed.xml")
    await pending_feed_input(follow_up, context)

    follow_up.effective_message.reply_text.assert_awaited_once_with(PENDING_EXPIRED)
    assert PENDING_FEED_KEY not in context.user_data


@pytest.mark.anyio
async def test_pending_input_is_scoped_to_the_chat() -> None:
    context = _make_context(AsyncMock())
    await add_feed_command(_make_update(chat_id=100), context)

    other_chat = _make_update(chat_id=200, text="https://example.com/feed.xml")
    await pending_feed_input(other_chat, context)

    other_chat.effective_message.reply_text.assert_not_awaited()
    assert PENDING_FEED_KEY not in context.user_data


@pytest.mark.anyio
async def test_pending_input_is_scoped_to_the_user() -> None:
    context = _make_context(AsyncMock())
    await add_feed_command(_make_update(telegram_user_id=111, chat_id=100), context)

    other_user = _make_update(
        telegram_user_id=222,
        chat_id=100,
        text="https://example.com/feed.xml",
    )
    await pending_feed_input(other_user, context)

    other_user.effective_message.reply_text.assert_not_awaited()
    assert PENDING_FEED_KEY not in context.user_data


@pytest.mark.anyio
async def test_delete_feed_two_step_by_url(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            user = await create_user(session, _TEST_TELEGRAM_USER_ID)
            await create_feed(
                session,
                user.id,
                "https://example.com/feed.xml",
                title="Example Feed",
            )

        context = _make_context(db)
        await delete_feed_command(_make_update(), context)

        follow_up = _make_update(text="https://example.com/feed.xml")
        await pending_feed_input(follow_up, context)

        follow_up.effective_message.reply_text.assert_awaited_once_with(
            DEL_FEED_SUCCESS_URL.format(url="https://example.com/feed.xml")
        )
        assert await use_cases.list_user_feeds(db, _TEST_TELEGRAM_USER_ID) == []
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_delete_feed_two_step_by_id(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            user = await create_user(session, _TEST_TELEGRAM_USER_ID)
            feed = await create_feed(
                session,
                user.id,
                "https://example.com/feed.xml",
                title="Example Feed",
            )
            feed_id = feed.id

        context = _make_context(db)
        await delete_feed_command(_make_update(), context)

        follow_up = _make_update(text=str(feed_id))
        await pending_feed_input(follow_up, context)

        assert "حذف شد" in follow_up.effective_message.reply_text.await_args.args[0]
        assert await use_cases.list_user_feeds(db, _TEST_TELEGRAM_USER_ID) == []
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_delete_feed_by_url_cannot_delete_another_users_feed(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        owner_telegram_id = 999_000_111
        async with db.session() as session:
            owner = await create_user(session, owner_telegram_id)
            await create_feed(
                session,
                owner.id,
                "https://example.com/owner/feed.xml",
                title="Owner Feed",
            )

        context = _make_context(db)
        await delete_feed_command(_make_update(), context)
        follow_up = _make_update(text="https://example.com/owner/feed.xml")
        await pending_feed_input(follow_up, context)

        assert "یافت نشد" in follow_up.effective_message.reply_text.await_args.args[0]
        assert len(await use_cases.list_user_feeds(db, owner_telegram_id)) == 1
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_delete_feed_direct_url_is_backward_compatible(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            user = await create_user(session, _TEST_TELEGRAM_USER_ID)
            await create_feed(
                session,
                user.id,
                "https://example.com/feed.xml",
                title="Example Feed",
            )

        update = _make_update()
        await delete_feed_command(
            update,
            _make_context(db, args=["https://example.com/feed.xml"]),
        )

        update.effective_message.reply_text.assert_awaited_once_with(
            DEL_FEED_SUCCESS_URL.format(url="https://example.com/feed.xml")
        )
    finally:
        await db.dispose()


# --- /ask coordination lease ------------------------------------------------


class _LeaseInspectingAgent:
    """Reads the persisted lease while the answer is being produced."""

    def __init__(self, db: Database, error: Exception | None = None) -> None:
        self._db = db
        self._error = error
        self.lease_seen: datetime | None = None

    async def answer(self, user_id, question: str) -> AgentAnswer:
        async with self._db.session() as session:
            user = await session.get(User, user_id)
            assert user is not None
            self.lease_seen = user.agent_processing_until
        if self._error is not None:
            raise self._error
        return _grounded_answer()


@pytest.mark.anyio
async def test_ask_command_holds_a_bounded_lease_and_releases_it(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            user = await create_user(session, _TEST_TELEGRAM_USER_ID)
            user_id = user.id

        agent = _LeaseInspectingAgent(db)
        before = datetime.now(UTC)

        await ask_command(
            _make_update(),
            _make_context(db, args=["سؤال"], agent=agent),
        )

        assert agent.lease_seen is not None
        lease = agent.lease_seen.replace(tzinfo=UTC)
        assert (
            before
            < lease
            <= before + timedelta(seconds=use_cases.AGENT_LEASE_SECONDS + 5)
        )

        async with db.session() as session:
            stored = await session.get(User, user_id)
            assert stored is not None
            assert stored.agent_processing_until is None
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_ask_command_releases_the_lease_on_agent_failure(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            user = await create_user(session, _TEST_TELEGRAM_USER_ID)
            user_id = user.id

        agent = _LeaseInspectingAgent(db, error=RuntimeError("boom"))
        await ask_command(
            _make_update(),
            _make_context(db, args=["سؤال"], agent=agent),
        )

        assert agent.lease_seen is not None
        async with db.session() as session:
            stored = await session.get(User, user_id)
            assert stored is not None
            assert stored.agent_processing_until is None
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_recipient_reactivation_clears_the_unavailable_status(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            user = await create_user(session, _TEST_TELEGRAM_USER_ID)
            user.delivery_unavailable_at = datetime.now(UTC)

        update = _make_update()
        await mark_recipient_available_handler(update, _make_context(db))

        async with db.session() as session:
            stored = await get_user_by_telegram_id(session, _TEST_TELEGRAM_USER_ID)
            assert stored is not None
            assert stored.delivery_unavailable_at is None
        update.effective_message.reply_text.assert_not_awaited()
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_recipient_reactivation_does_not_create_a_user(tmp_path) -> None:
    db = await _make_real_db(tmp_path)
    try:
        await mark_recipient_available_handler(_make_update(), _make_context(db))

        async with db.session() as session:
            assert (
                await get_user_by_telegram_id(session, _TEST_TELEGRAM_USER_ID) is None
            )
    finally:
        await db.dispose()


# --- schedule command edge cases -------------------------------------------


@pytest.mark.anyio
async def test_schedule_command_rejects_the_off_alias() -> None:
    """`off` belongs to /delivery, not to the interval parser."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=["off"])
    _mock_db_session(context.application.bot_data["db"])

    with patch("ai_rss_news_assistant.bot.use_cases.set_delivery_interval") as mock_set:
        await schedule_command(update, context)

    mock_set.assert_not_called()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "بازه‌های مجاز" in text


@pytest.mark.anyio
async def test_schedule_command_accepts_the_english_immediate_alias() -> None:
    update = _make_update()
    context = _make_context(AsyncMock(), args=["immediate"])
    db = context.application.bot_data["db"]
    _mock_db_session(db)

    with patch(
        "ai_rss_news_assistant.bot.use_cases.set_delivery_interval",
        new_callable=AsyncMock,
        return_value=0,
    ) as mock_set:
        await schedule_command(update, context)

    mock_set.assert_awaited_once_with(db, _TEST_TELEGRAM_USER_ID, 0)


@pytest.mark.anyio
async def test_help_documents_two_step_flows_and_the_toggle() -> None:
    update = _make_update()
    await help_command(update, _make_context(AsyncMock()))

    text = update.effective_message.reply_text.call_args[0][0]
    assert "/cancel" in text
    assert "/delivery" in text
    assert "/addfeed [url]" in text
    assert "/delfeed [id|url]" in text
    for token in ("0", "15m", "6h", "24h"):
        assert token in text


@pytest.mark.anyio
async def test_start_documents_the_canonical_schedule_and_cancel() -> None:
    update = _make_update()
    context = _make_context(AsyncMock())

    with patch(
        "ai_rss_news_assistant.bot.handlers.use_cases.ensure_user_id",
        new_callable=AsyncMock,
    ):
        await start_command(update, context)

    text = update.effective_message.reply_text.call_args[0][0]
    assert "/schedule" in text
    assert "/cancel" in text
    for token in ("0", "15m", "6h", "24h"):
        assert token in text
