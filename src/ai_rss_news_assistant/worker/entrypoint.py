"""Independent worker process entry point.

Runs the periodic RSS ingestion loop, full-text extraction, summarization, and
automatic delivery. This is the ``worker`` service: it is started by
``python -m ai_rss_news_assistant.worker``, by the bot-facing CLI's ``worker``
command, and by Compose. Nothing in the bot or agent process imports this
module at runtime.

A missing or rejected ``BOT_TOKEN`` disables automatic delivery but must never
stop ingestion, so the token is optional here and failures warn instead of
raising.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from telegram import Bot
from telegram.error import InvalidToken

from ai_rss_news_assistant.config import WorkerSettings
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.logging import setup_logging
from ai_rss_news_assistant.worker.delivery import DeliveryService
from ai_rss_news_assistant.worker.service import NewsWorker
from ai_rss_news_assistant.worker.summarization import build_summarizer

logger = logging.getLogger(__name__)


def run_worker() -> None:
    """Initialize the worker process: ingestion, summarization, and delivery.

    The worker sends automatic delivery messages, so it uses ``BOT_TOKEN`` when
    it is configured. Without a token it still ingests news; delivery is
    disabled and a warning is logged.
    """
    # The worker validates only the settings it needs: it can ingest and
    # summarize without BOT_TOKEN, so a missing token warns instead of failing.
    settings = WorkerSettings()
    setup_logging(settings.log_level)
    logger.info("worker process starting (app_env=%s)", settings.app_env)

    db = Database(settings.database_url)
    poll_interval = settings.poll_interval_seconds
    fetch_full_text = settings.fetch_full_text
    fetch_timeout = settings.fetch_timeout_seconds

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    bot: Bot | None = None
    if settings.bot_token is not None:
        try:
            bot = Bot(settings.bot_token)
        except InvalidToken:
            # The exception message contains the rejected token; never log it.
            logger.error("Telegram rejected BOT_TOKEN; automatic delivery is disabled")
            bot = None
        else:
            try:
                loop.run_until_complete(bot.initialize())
            except InvalidToken:
                # The exception message contains the rejected token; never log it.
                logger.error(
                    "Telegram rejected BOT_TOKEN; automatic delivery is disabled"
                )
                try:
                    loop.run_until_complete(bot.shutdown())
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    logger.debug("Telegram bot cleanup after invalid token failed")
                bot = None
            except Exception:  # noqa: BLE001 - connectivity failures must not stop ingestion
                # Connectivity problems must not stop RSS ingestion. Delivery
                # attempts fail per item and are retried on the next poll.
                logger.warning(
                    "Could not initialize the Telegram bot; delivery attempts will "
                    "retry on each poll"
                )
    else:
        logger.warning("BOT_TOKEN is not set; automatic delivery is disabled")

    delivery_service = (
        DeliveryService(
            session_factory=db.session_factory,
            bot=bot,
            summarizer=build_summarizer(settings),
        )
        if bot is not None
        else None
    )

    news_worker = NewsWorker(
        engine=db.engine,
        poll_interval_seconds=poll_interval,
        fetch_full_text=fetch_full_text,
        fetch_timeout=fetch_timeout,
        delivery_service=delivery_service,
    )

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
        if bot is not None:
            try:
                loop.run_until_complete(bot.shutdown())
            except Exception:
                logger.exception("Failed to shut down the Telegram bot")
        loop.run_until_complete(db.dispose())
        loop.close()
