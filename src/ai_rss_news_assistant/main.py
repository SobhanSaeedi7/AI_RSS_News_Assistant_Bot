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

from ai_rss_news_assistant.config import Settings
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.db.rss_worker import NewsWorker
from ai_rss_news_assistant.logging import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Initialize the bot process: configuration and logging only.

    The bot service requires a Telegram bot token; it is validated here so the
    worker process can start without it.
    """
    settings = Settings()
    if settings.bot_token is None:
        raise ValueError("BOT_TOKEN is required for the bot service")
    setup_logging(settings.log_level)
    logger.info("bot process starting (app_env=%s)", settings.app_env)


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
