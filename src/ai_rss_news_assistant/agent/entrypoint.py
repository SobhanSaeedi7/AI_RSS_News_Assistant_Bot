"""Agent process entry point: serve the internal HTTP API with Uvicorn.

The agent reads stored news for one user at a time and answers from that
evidence only. It never touches Telegram, never ingests RSS, and never fetches
anything from the web; the only outbound call it can make is the provider chat
completion.

Startup validates only the settings the agent needs. A missing
``AGENT_INTERNAL_TOKEN`` does not stop the process -- the API answers 503
instead of serving unauthenticated requests -- so an operator sees the reason in
the logs and in the bot's replies rather than a crash loop.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI

from ai_rss_news_assistant.agent.answer import NewsAgent
from ai_rss_news_assistant.agent.api import create_app
from ai_rss_news_assistant.config import AgentSettings
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.llm import build_chat_client
from ai_rss_news_assistant.logging import setup_logging

logger = logging.getLogger(__name__)


def build_app(settings: AgentSettings, db: Database) -> FastAPI:
    """Create the API application wired to the real database and provider."""

    @contextlib.asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await db.dispose()

    answerer = NewsAgent(
        session_factory=db.session_factory,
        client=build_chat_client(settings),
    )
    return create_app(settings=settings, answerer=answerer, lifespan=_lifespan)


def run_agent() -> None:
    """Initialize the agent process and serve its internal HTTP API."""
    settings = AgentSettings()
    setup_logging(settings.log_level)
    logger.info("agent process starting (app_env=%s)", settings.app_env)

    if settings.agent_internal_token is None:
        logger.error(
            "AGENT_INTERNAL_TOKEN is not set; every agent request will be refused"
        )

    db = Database(settings.database_url)
    uvicorn.run(
        build_app(settings, db),
        host=settings.agent_host,
        port=settings.agent_port,
        log_level=settings.log_level.lower(),
    )
