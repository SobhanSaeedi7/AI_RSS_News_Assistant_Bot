"""Application configuration loaded from environment variables.

All settings are read from the environment (or a ``.env`` file in local
development) and validated at startup. Secrets are never hard-coded; required
secrets such as ``bot_token`` cause a clear, early failure when missing.
"""

from __future__ import annotations

import logging

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LLM_TIMEOUT_SECONDS = 30


class Settings(BaseSettings):
    """Runtime configuration for the AI RSS News Assistant."""

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

    @field_validator("bot_token", mode="before")
    @classmethod
    def _normalize_bot_token(cls, value: object) -> object:
        """Treat an empty or whitespace-only token as unset."""
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("llm_timeout_seconds")
    @classmethod
    def _validate_llm_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("llm_timeout_seconds must be a positive integer")
        return value

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
