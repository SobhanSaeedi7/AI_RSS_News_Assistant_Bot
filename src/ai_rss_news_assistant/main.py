"""Compatibility facade for the historical ``ai_rss_news_assistant.main`` module.

The bot process now lives in :mod:`ai_rss_news_assistant.bot.entrypoint` and the
worker process in :mod:`ai_rss_news_assistant.worker.entrypoint`. This module
keeps the long-documented ``main`` and ``worker`` names working for the
``ai-rss-news-assistant`` console script and for
``python -m ai_rss_news_assistant``, so existing deployments and callers keep
working. New code should import the service packages directly.
"""

from __future__ import annotations

from ai_rss_news_assistant.bot.entrypoint import run_bot
from ai_rss_news_assistant.worker.entrypoint import run_worker


def main() -> None:
    """Run the bot process (historical entry-point name)."""
    run_bot()


def worker() -> None:
    """Run the worker process (historical entry-point name)."""
    run_worker()


__all__ = ["main", "worker"]
