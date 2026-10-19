"""Application entry point shared by the bot and worker processes.

This module only wires up configuration and logging. The bot (Telegram
interaction) and worker (RSS polling, summarization, delivery) behaviors are
deliberately not implemented yet; they are tracked as future milestones.
"""

from __future__ import annotations

import logging

from .config import Settings
from .logging import setup_logging

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
    BOT_TOKEN. RSS polling, summarization, and scheduled delivery are
    implemented in a later milestone and are intentionally absent here.
    """
    settings = Settings()
    setup_logging(settings.log_level)
    logger.info("worker process starting (app_env=%s)", settings.app_env)
