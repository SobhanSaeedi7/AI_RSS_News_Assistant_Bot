"""Application entry point shared by the bot and worker processes.

This module only wires up configuration and logging. The bot (Telegram
interaction) and worker (RSS polling, summarization, delivery) behaviors
are implemented here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from ai_rss_news_assistant.config import Settings
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.db.repository import create_user, get_user_by_telegram_id
from ai_rss_news_assistant.db.rss_worker import NewsWorker
from ai_rss_news_assistant.logging import setup_logging

logger = logging.getLogger(__name__)

WELCOME_MESSAGE = (
    "سلام! به ر봤다 خبری RSS خوش آمدید.\n\n"
    "دستورهای موجود:\n"
    "/start - ثبت نام و شروع کار با ر봤다\n"
    "/help - نمایش راهنما"
)

HELP_MESSAGE = (
    "راهنما ر봤다 خبری RSS\n\n"
    "دستورهای موجود:\n"
    "/start - ثبت نام و شروع کار با ر봤다\n"
    "/help - نمایش راهنما"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start: find or create user, then welcome them."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    db: Database = context.application.bot_data["db"]
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if user is None:
            await create_user(session, telegram_user.id)

    await update.effective_message.reply_text(WELCOME_MESSAGE)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help: show available commands."""
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(HELP_MESSAGE)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by Updates without leaking secrets."""
    logger.exception("Exception while handling an update: %s", context.error)


def main() -> None:
    """Initialize the bot process and run Telegram polling."""
    settings = Settings()
    if settings.bot_token is None:
        raise ValueError("BOT_TOKEN is required for the bot service")
    setup_logging(settings.log_level)
    logger.info("bot process starting (app_env=%s)", settings.app_env)

    db = Database(settings.database_url)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = None
    try:
        application = (
            ApplicationBuilder()
            .token(settings.bot_token)
            .build()
        )
        application.bot_data["db"] = db

        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_error_handler(_error_handler)

        try:
            loop.add_signal_handler(signal.SIGINT, loop.stop)
            loop.add_signal_handler(signal.SIGTERM, loop.stop)
        except (NotImplementedError, RuntimeError):
            # Signal handlers are not supported on this platform (e.g., Windows).
            # The worker will still run; it just can't be stopped via signals.
            logger.warning("Signal handlers not supported on this platform")

        loop.run_until_complete(application.initialize())
        loop.run_until_complete(application.start())
        loop.run_until_complete(application.updater.start_polling())
        loop.run_forever()
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


def worker() -> None:
    """Initialize the worker process: configuration and logging only.

    The worker service does not interact with Telegram, so it does not require
    BOT_TOKEN. RSS polling, summarization, and scheduled delivery run here.
    """
    settings = Settings()
    setup_logging(settings.log_level)
    logger.info("worker process starting (app_env=%s)", settings.app_env)

    db = Database(settings.database_url)
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
    fetch_full_text = os.environ.get("FETCH_FULL_TEXT", "true").lower() == "true"
    fetch_timeout = int(os.environ.get("FETCH_TIMEOUT_SECONDS", "30"))

    news_worker = NewsWorker(
        engine=db.engine,
        poll_interval_seconds=poll_interval,
        fetch_full_text=fetch_full_text,
        fetch_timeout=fetch_timeout,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.add_signal_handler(
            signal.SIGTERM, lambda: asyncio.ensure_future(news_worker.stop())
        )
        loop.add_signal_handler(
            signal.SIGINT, lambda: asyncio.ensure_future(news_worker.stop())
        )
    except (NotImplementedError, RuntimeError):
        # Signal handlers are not supported on this platform (e.g., Windows).
        # The worker will still run; it just can't be stopped via signals.
        logger.warning("Signal handlers not supported on this platform")

    try:
        loop.run_until_complete(news_worker.run())
    finally:
        loop.run_until_complete(db.dispose())
        loop.close()
