"""Application configuration loaded from environment variables.

All settings are read from the environment (or a ``.env`` file in local
development) and validated at startup. Secrets are never hard-coded; required
secrets such as ``bot_token`` cause a clear, early failure when missing.

:class:`Settings` holds only what every process needs. Each service then uses
its own subclass -- :class:`BotSettings`, :class:`WorkerSettings`, or
:class:`AgentSettings` -- so a service never fails to start because of a
setting only another service uses, and every environment variable keeps the
name it already had.
"""

from __future__ import annotations

import logging

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LLM_TIMEOUT_SECONDS = 30
DEFAULT_AGENT_PORT = 8000
DEFAULT_AGENT_TIMEOUT_SECONDS = 40
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_FETCH_TIMEOUT_SECONDS = 30


def _positive(value: int, name: str) -> int:
    """Reject a non-positive duration or count with the setting's own name."""
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class Settings(BaseSettings):
    """Settings shared by every service.

    Only ``database_url`` is required here: the bot and the agent both read and
    write stored news, so both need it. Service-specific requirements live in
    the subclasses below.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Telegram ---
    bot_token: str | None = Field(
        None,
        description=(
            "Telegram bot token provided by @BotFather. Required by the bot "
            "service and by the worker to send automatic delivery messages."
        ),
    )

    # --- Application ---
    app_env: str = Field(
        "development",
        description="Deployment environment (development, staging, production)",
    )
    log_level: str = Field(
        "INFO",
        description="Application log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )

    # --- Database ---
    database_url: str = Field(
        ...,
        description="PostgreSQL connection URL shared by the bot and worker",
    )

    # --- LLM ---
    llm_api_key: str | None = Field(
        None,
        description=(
            "API key for the OpenAI-compatible LLM provider. Optional; "
            "delivery falls back to extractive summaries and /ask reports that "
            "AI answers are disabled when unset."
        ),
    )
    llm_base_url: str = Field(
        "https://api.avalai.ir/v1",
        description="Base URL of the OpenAI-compatible chat completions API",
    )
    llm_model: str = Field(
        "gpt-6-luna",
        description="Model used to summarize news items and answer /ask questions",
    )
    llm_timeout_seconds: int = Field(
        DEFAULT_LLM_TIMEOUT_SECONDS,
        description="Timeout in seconds for a single LLM request",
    )

    # --- Outbound fetching ---
    fetch_timeout_seconds: int = Field(
        DEFAULT_FETCH_TIMEOUT_SECONDS,
        description=(
            "Timeout in seconds for fetching a feed (bot add-time probe and "
            "worker polling) or an article's full text"
        ),
    )

    @field_validator("bot_token", "agent_internal_token", mode="before")
    @classmethod
    def _normalize_optional_secret(cls, value: object) -> object:
        """Treat an empty or whitespace-only secret as unset.

        Compose passes an empty string for an unset variable, and an empty
        token must never be treated as a valid credential.
        """
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("llm_timeout_seconds")
    @classmethod
    def _validate_llm_timeout(cls, value: int) -> int:
        return _positive(value, "llm_timeout_seconds")

    @field_validator("fetch_timeout_seconds")
    @classmethod
    def _validate_fetch_timeout(cls, value: int) -> int:
        return _positive(value, "fetch_timeout_seconds")

    @field_validator("app_env")
    @classmethod
    def _validate_app_env(cls, value: str) -> str:
        allowed = {"development", "staging", "production"}
        normalized = value.lower()
        if normalized not in allowed:
            raise ValueError(f"app_env must be one of {sorted(allowed)}, got {value!r}")
        return normalized

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in logging._nameToLevel:
            raise ValueError(f"log_level must be a valid logging level, got {value!r}")
        return normalized

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "database_url must use the asyncpg driver (postgresql+asyncpg://)"
            )
        return value

    # --- Agent service (shared by the agent itself and its bot caller) ---
    agent_internal_token: str | None = Field(
        None,
        description=(
            "Shared bearer token for the internal agent API. Required in "
            "production; the agent refuses every request until it is set."
        ),
    )


class BotSettings(Settings):
    """Settings the bot service needs.

    ``bot_token`` stays optional here and is checked by the bot entry point so
    the failure is a single explicit message; the agent settings are optional
    so the bot still starts (with /ask reporting that answers are unavailable)
    when the agent is not configured.
    """

    agent_base_url: str = Field(
        f"http://agent:{DEFAULT_AGENT_PORT}",
        description="Base URL of the internal agent API",
    )
    agent_timeout_seconds: int = Field(
        DEFAULT_AGENT_TIMEOUT_SECONDS,
        description=(
            "Outer timeout for one agent request. Deliberately larger than the "
            "LLM timeout so the agent can report its own provider timeout."
        ),
    )

    @field_validator("agent_timeout_seconds")
    @classmethod
    def _validate_agent_timeout(cls, value: int) -> int:
        return _positive(value, "agent_timeout_seconds")


class WorkerSettings(Settings):
    """Settings the worker service needs: ingestion, summarization, delivery."""

    poll_interval_seconds: int = Field(
        DEFAULT_POLL_INTERVAL_SECONDS,
        description="Seconds between RSS polling runs",
    )
    fetch_full_text: bool = Field(
        True,
        description="Fetch and store full article text for items that only have a summary",
    )

    @field_validator("poll_interval_seconds")
    @classmethod
    def _validate_poll_interval(cls, value: int) -> int:
        return _positive(value, "poll_interval_seconds")


class AgentSettings(Settings):
    """Settings the agent service needs.

    ``agent_internal_token`` is optional so the process always starts; until it
    is configured the API answers 503 instead of serving unauthenticated
    requests. ``llm_api_key`` stays optional for the same reason.
    """

    agent_host: str = Field("0.0.0.0", description="Bind address of the agent API")
    agent_port: int = Field(
        DEFAULT_AGENT_PORT, description="Bind port of the agent API"
    )

    @field_validator("agent_port")
    @classmethod
    def _validate_agent_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("agent_port must be a valid TCP port")
        return value
