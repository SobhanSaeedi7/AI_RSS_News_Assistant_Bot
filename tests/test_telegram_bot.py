"""Tests for the Telegram bot entry point and handlers.

Mocks Telegram and database dependencies; no real token, network, or live
database is required.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update
from telegram.ext import Application, ContextTypes

from ai_rss_news_assistant.db.models import User
from ai_rss_news_assistant.main import help_command, start_command

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


def _make_context(db: MagicMock) -> MagicMock:
    """Create a mock ContextTypes.DEFAULT_TYPE with application.bot_data."""
    application = MagicMock(spec=Application)
    application.bot_data = {"db": db}
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.application = application
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

        # db.dispose() coroutine was passed to loop.run_until_complete
        assert mock_loop.run_until_complete.called
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

        # db.dispose() coroutine was passed to loop.run_until_complete
        assert mock_loop.run_until_complete.called
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

        # db.dispose() coroutine was passed to loop.run_until_complete exactly once
        dispose_coroutines = [
            call[0][0]
            for call in mock_loop.run_until_complete.call_args_list
            if asyncio.iscoroutine(call[0][0])
        ]
        assert len(dispose_coroutines) == 1
        mock_loop.close.assert_called_once()
