"""Application configuration loaded from environment variables.

All settings are read from the environment (or a ``.env`` file in local
development) and validated at startup. Secrets are never hard-coded; required
secrets such as ``bot_token`` cause a clear, early failure when missing.
"""

from __future__ import annotations

import logging

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the AI RSS News Assistant."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # --- Telegram ---
    bot_token: str = Field(
        ...,
        description="Telegram bot token provided by @BotFather",
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
        if not value.startswith(("postgresql+", "postgres://")):
            raise ValueError("database_url must be a PostgreSQL URL")
        return value
