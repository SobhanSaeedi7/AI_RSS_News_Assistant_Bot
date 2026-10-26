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

from ai_rss_news_assistant.agent import AgentAnswer, AgentSource
from ai_rss_news_assistant.db.base import Base, Database
from ai_rss_news_assistant.db.models import User, UserFeed
from ai_rss_news_assistant.db.repository import (
    create_feed,
    create_news_item,
    create_user,
    get_user_by_telegram_id,
)
from ai_rss_news_assistant.main import (
    AGENT_ERROR,
    ASK_TOO_LONG,
    ASK_USAGE,
    DELIVERY_OFF,
    DELIVERY_ON,
    DELIVERY_STATUS_OFF,
    DELIVERY_STATUS_ON,
    DELIVERY_USAGE,
    NEWS_EMPTY,
    NEWS_MAX_MESSAGE_LENGTH,
    _feed_display_name,
    add_feed_command,
    ask_command,
    build_ask_messages,
    delete_feed_command,
    delivery_command,
    help_command,
    list_feeds_command,
    news_command,
    start_command,
)

_TEST_TELEGRAM_USER_ID = 123456789
_TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
_TEST_DATABASE_URL = "postgresql+asyncpg://ai_rss_news:secret@db:5432/ai_rss_news"


def _make_update(telegram_user_id: int = _TEST_TELEGRAM_USER_ID) -> MagicMock:
    """Create a mock Update with a Telegram user and message."""
    user = MagicMock()
    user.id = telegram_user_id
    user.is_bot = False
    user.first_name = "Test"

    message = MagicMock()
    message.reply_text = AsyncMock()

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
    return context


def _mock_db_session(db: MagicMock) -> AsyncMock:
    """Configure db.session() to return an async context manager yielding a session."""
    mock_session = AsyncMock()
    session_manager = AsyncMock()
    session_manager.__aenter__.return_value = mock_session
    session_manager.__aexit__.return_value = None
    db.session = MagicMock(return_value=session_manager)
    return mock_session


@pytest.mark.anyio
async def test_start_command_creates_new_user() -> None:
    """Test /start creates a new user when none exists."""
    update = _make_update()
    context = _make_context(AsyncMock())

    mock_session = _mock_db_session(context.application.bot_data["db"])

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=None,
    ) as mock_get, patch(
        "ai_rss_news_assistant.main.create_user",
        return_value=MagicMock(spec=User),
    ) as mock_create:
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=existing_user,
    ) as mock_get, patch(
        "ai_rss_news_assistant.main.create_user",
    ) as mock_create:
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[],
    ), patch(
        "ai_rss_news_assistant.main.create_feed",
        return_value=new_feed,
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id"
    ) as mock_get_user, patch(
        "ai_rss_news_assistant.main.normalize_url"
    ) as mock_normalize:
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[existing_feed],
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[],
    ), patch(
        "ai_rss_news_assistant.main.create_feed",
        side_effect=exc,
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[],
    ), patch(
        "ai_rss_news_assistant.main.create_feed",
        side_effect=exc,
    ), pytest.raises(IntegrityError):
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[],
    ), patch(
        "ai_rss_news_assistant.main.create_feed",
        side_effect=exc,
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[],
    ), patch(
        "ai_rss_news_assistant.main.create_feed",
        side_effect=exc,
    ), pytest.raises(IntegrityError):
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
    mock_session = context.application.bot_data["db"].session.return_value.__aenter__.return_value

    orig = MagicMock()
    orig.pgcode = "23505"
    orig.constraint_name = "uq_user_feeds_user_id_url"
    exc = IntegrityError("duplicate key violates unique constraint", None, orig)

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[],
    ), patch(
        "ai_rss_news_assistant.main.create_feed",
        side_effect=exc,
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[],
    ), patch(
        "ai_rss_news_assistant.main.create_feed",
        side_effect=exc,
    ):
        await add_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "قبلاً" in text


@pytest.mark.anyio
async def test_list_feeds_empty() -> None:
    """Test /listfeeds with no feeds shows empty message."""
    update = _make_update()
    context = _make_context(AsyncMock())

    _mock_db_session(context.application.bot_data["db"])

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[],
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[feed1, feed2],
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.list_feeds",
        return_value=[feed, feed, feed],
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.delete_feed",
        return_value=True,
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

    with patch(
        "ai_rss_news_assistant.main.get_user_by_telegram_id",
        return_value=MagicMock(spec=User),
    ), patch(
        "ai_rss_news_assistant.main.delete_feed",
        return_value=False,
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
async def test_delete_feed_missing_id() -> None:
    """Test /delfeed with no ID argument shows usage."""
    update = _make_update()
    context = _make_context(AsyncMock(), args=[])

    await delete_feed_command(update, context)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "شناسه فید را ارائه دهید" in text


def test_error_handler_does_not_leak_secrets(caplog) -> None:
    """Test _error_handler logs the error without exposing secrets."""
    context = MagicMock()
    context.error = Exception("database connection failed")

    with caplog.at_level(logging.ERROR, logger="ai_rss_news_assistant.main"):
        from ai_rss_news_assistant.main import _error_handler

        asyncio.run(_error_handler(None, context))

    assert "database connection failed" in caplog.text
    assert _TEST_BOT_TOKEN not in caplog.text
    assert "secret" not in caplog.text.lower()


def test_main_disposes_db_on_application_build_failure() -> None:
    """Database engine is disposed if Application construction fails."""
    mock_db = MagicMock()
    mock_db.dispose = AsyncMock()
    mock_loop = MagicMock()

    with patch(
        "ai_rss_news_assistant.main.Database", return_value=mock_db
    ), patch(
        "ai_rss_news_assistant.main.ApplicationBuilder"
    ) as mock_builder, patch(
        "ai_rss_news_assistant.main.asyncio.new_event_loop",
        return_value=mock_loop,
    ), patch(
        "ai_rss_news_assistant.main.asyncio.set_event_loop"
    ), patch.dict(
        "os.environ",
        {"BOT_TOKEN": _TEST_BOT_TOKEN, "DATABASE_URL": _TEST_DATABASE_URL},
        clear=True,
    ):
        mock_builder.return_value.token.return_value.build.side_effect = RuntimeError(
            "build failed"
        )

        with pytest.raises(RuntimeError, match="build failed"):
            from ai_rss_news_assistant.main import main

            main()

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

    with patch(
        "ai_rss_news_assistant.main.Database", return_value=mock_db
    ), patch(
        "ai_rss_news_assistant.main.ApplicationBuilder"
    ) as mock_builder, patch(
        "ai_rss_news_assistant.main.asyncio.new_event_loop",
        return_value=mock_loop,
    ), patch(
        "ai_rss_news_assistant.main.asyncio.set_event_loop"
    ), patch.dict(
        "os.environ",
        {"BOT_TOKEN": _TEST_BOT_TOKEN, "DATABASE_URL": _TEST_DATABASE_URL},
        clear=True,
    ):
        mock_builder.return_value.token.return_value.build.return_value = (
            mock_application
        )
        mock_loop.run_forever.side_effect = RuntimeError("polling failed")

        with pytest.raises(RuntimeError, match="polling failed"):
            from ai_rss_news_assistant.main import main

            main()

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

    with patch(
        "ai_rss_news_assistant.main.Database", return_value=mock_db
    ), patch(
        "ai_rss_news_assistant.main.ApplicationBuilder"
    ) as mock_builder, patch(
        "ai_rss_news_assistant.main.asyncio.new_event_loop",
        return_value=mock_loop,
    ), patch(
        "ai_rss_news_assistant.main.asyncio.set_event_loop"
    ), patch.dict(
        "os.environ",
        {"BOT_TOKEN": _TEST_BOT_TOKEN, "DATABASE_URL": _TEST_DATABASE_URL},
        clear=True,
    ):
        mock_builder.return_value.token.return_value.build.return_value = (
            mock_application
        )
        mock_loop.run_forever.side_effect = KeyboardInterrupt()

        from ai_rss_news_assistant.main import main

        main()

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

    with patch(
        "ai_rss_news_assistant.main.Database", return_value=mock_db
    ), patch(
        "ai_rss_news_assistant.main.ApplicationBuilder"
    ) as mock_builder, patch(
        "ai_rss_news_assistant.main.setup_logging"
    ), patch(
        "ai_rss_news_assistant.main.asyncio.new_event_loop",
        return_value=mock_loop,
    ), patch(
        "ai_rss_news_assistant.main.asyncio.set_event_loop"
    ), patch.dict(
        "os.environ",
        {"BOT_TOKEN": _TEST_BOT_TOKEN, "DATABASE_URL": _TEST_DATABASE_URL},
        clear=True,
    ), caplog.at_level(
        logging.ERROR, logger="ai_rss_news_assistant.main"
    ):
        mock_builder.return_value.token.return_value.build.return_value = (
            mock_application
        )

        from ai_rss_news_assistant.main import main

        main()

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
async def test_delivery_command_defaults_to_on(tmp_path) -> None:
    """A new user sees automatic delivery enabled by default."""
    db = await _make_real_db(tmp_path)
    try:
        update = _make_update()
        context = _make_context(db)

        await delivery_command(update, context)

        update.effective_message.reply_text.assert_awaited_once_with(DELIVERY_STATUS_ON)
        assert await _delivery_preference(db, _TEST_TELEGRAM_USER_ID) is True
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_delivery_command_turns_off_and_persists(tmp_path) -> None:
    """`/delivery off` is persisted and reflected on the next invocation."""
    db = await _make_real_db(tmp_path)
    try:
        update = _make_update()
        await delivery_command(update, _make_context(db, args=["off"]))

        update.effective_message.reply_text.assert_awaited_once_with(DELIVERY_OFF)
        assert await _delivery_preference(db, _TEST_TELEGRAM_USER_ID) is False

        status_update = _make_update()
        await delivery_command(status_update, _make_context(db))
        status_update.effective_message.reply_text.assert_awaited_once_with(
            DELIVERY_STATUS_OFF
        )
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_delivery_command_turns_on(tmp_path) -> None:
    """`/delivery on` re-enables delivery for a user who disabled it."""
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            await create_user(
                session, _TEST_TELEGRAM_USER_ID, auto_delivery_enabled=False
            )

        update = _make_update()
        await delivery_command(update, _make_context(db, args=["ON"]))

        update.effective_message.reply_text.assert_awaited_once_with(DELIVERY_ON)
        assert await _delivery_preference(db, _TEST_TELEGRAM_USER_ID) is True
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_delivery_command_rejects_invalid_argument(tmp_path) -> None:
    """An unknown argument shows usage and leaves the preference untouched."""
    db = await _make_real_db(tmp_path)
    try:
        async with db.session() as session:
            await create_user(session, _TEST_TELEGRAM_USER_ID)

        update = _make_update()
        await delivery_command(update, _make_context(db, args=["maybe"]))

        update.effective_message.reply_text.assert_awaited_once_with(DELIVERY_USAGE)
        assert await _delivery_preference(db, _TEST_TELEGRAM_USER_ID) is True
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_delivery_preference_is_per_user(tmp_path) -> None:
    """Disabling delivery for one user does not affect another user."""
    db = await _make_real_db(tmp_path)
    try:
        other_telegram_id = _TEST_TELEGRAM_USER_ID + 1
        async with db.session() as session:
            await create_user(session, other_telegram_id)

        await delivery_command(_make_update(), _make_context(db, args=["off"]))

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
    from ai_rss_news_assistant.main import _split_message

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
