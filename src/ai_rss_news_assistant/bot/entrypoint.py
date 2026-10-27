"""Bot process entry point: Telegram polling and command wiring.

The bot is the only service that talks to Telegram: it registers the command
handlers and owns no retrieval, prompting, or summarization of its own.
Questions are answered by the object returned from :func:`_build_answerer` -- the
internal HTTP client for the agent service -- which exposes the
``answer(user_id, question)`` interface the handlers use. Nothing in this package
imports the agent's retrieval or LLM code, so the bot cannot call a provider
directly.

Startup validates only what the bot needs. A missing ``BOT_TOKEN`` fails with one
explicit error, while missing agent or LLM settings degrade the single affected
command instead of stopping the process.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from telegram.error import InvalidToken
from telegram.ext import ApplicationBuilder, CommandHandler

from ai_rss_news_assistant.bot.agent_client import (
    AgentHTTPClient,
    UnavailableAnswerer,
)
from ai_rss_news_assistant.bot.handlers import (
    _error_handler,
    add_feed_command,
    ask_command,
    delete_feed_command,
    delivery_command,
    help_command,
    list_feeds_command,
    news_command,
    start_command,
)
from ai_rss_news_assistant.bot.use_cases import Answerer
from ai_rss_news_assistant.config import BotSettings
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.logging import setup_logging

logger = logging.getLogger(__name__)


def _build_answerer(settings: BotSettings) -> Answerer:
    """Build the object /ask uses to answer questions for one user.

    The bot reaches the agent over internal HTTP; without a configured token it
    keeps running and /ask reports that answering is unavailable, so a missing
    token cannot take the whole bot down.
    """
    if settings.agent_internal_token is None:
        logger.warning(
            "AGENT_INTERNAL_TOKEN is not set; /ask will report that AI answers "
            "are unavailable"
        )
        return UnavailableAnswerer()

    return AgentHTTPClient(
        base_url=settings.agent_base_url,
        token=settings.agent_internal_token,
        timeout_seconds=settings.agent_timeout_seconds,
    )


def run_bot() -> None:
    """Initialize the bot process and run Telegram polling."""
    settings = BotSettings()
    if settings.bot_token is None:
        raise ValueError("BOT_TOKEN is required for the bot service")
    setup_logging(settings.log_level)
    logger.info("bot process starting (app_env=%s)", settings.app_env)

    db = Database(settings.database_url)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = None
    try:
        application = ApplicationBuilder().token(settings.bot_token).build()
        application.bot_data["db"] = db
        application.bot_data["agent"] = _build_answerer(settings)

        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("addfeed", add_feed_command))
        application.add_handler(CommandHandler("listfeeds", list_feeds_command))
        application.add_handler(CommandHandler("delfeed", delete_feed_command))
        application.add_handler(CommandHandler("news", news_command))
        application.add_handler(CommandHandler("delivery", delivery_command))
        application.add_handler(CommandHandler("ask", ask_command))
        application.add_error_handler(_error_handler)

        try:
            loop.add_signal_handler(signal.SIGINT, loop.stop)
            loop.add_signal_handler(signal.SIGTERM, loop.stop)
        except (NotImplementedError, RuntimeError):
            # Signal handlers are not supported on this platform (e.g., Windows).
            # The bot will still run; it just can't be stopped via signals.
            logger.warning("Signal handlers not supported on this platform")

        loop.run_until_complete(application.initialize())
        loop.run_until_complete(application.start())
        loop.run_until_complete(application.updater.start_polling())
        loop.run_forever()
    except InvalidToken:
        # InvalidToken messages can contain the rejected token; log a fixed
        # message instead of the exception text or traceback.
        logger.error("Telegram rejected BOT_TOKEN; the bot cannot start")
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application received stop signal. Shutting down.")
    finally:
        if application is not None:
            try:
                if application.updater.running:
                    loop.run_until_complete(application.updater.stop())
            except Exception:
                logger.exception("Failed to stop updater")
            try:
                if application.running:
                    loop.run_until_complete(application.stop())
                    loop.run_until_complete(application.shutdown())
            except Exception:
                logger.exception("Failed to stop application")
        loop.run_until_complete(db.dispose())
        loop.close()
