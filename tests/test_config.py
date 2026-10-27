"""Tests for the application configuration system.

These tests exercise the foundational configuration behavior: required settings
fail fast, optional fields have sensible defaults, and validators reject
invalid values. The bot requires ``BOT_TOKEN`` while the worker does not, so
each service validates only the settings it actually needs. No network,
Telegram, database, or LLM calls are made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from ai_rss_news_assistant.config import (
    AgentSettings,
    BotSettings,
    Settings,
    WorkerSettings,
)
from ai_rss_news_assistant.main import main, worker

_SETTINGS_CLASSES = (Settings, BotSettings, WorkerSettings, AgentSettings)


@pytest.fixture(autouse=True)
def _ignore_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not load values from the local .env file during tests.

    Tests set environment variables explicitly via monkeypatch. A pre-existing
    .env file (gitignored, not part of this repo) must not interfere. Each
    service settings class is patched separately because pydantic resolves
    ``model_config`` per class, so patching only the base class would leave the
    subclasses reading the real .env file.
    """
    config = SettingsConfigDict(
        env_file=None, env_file_encoding="utf-8", extra="ignore"
    )
    for settings_class in _SETTINGS_CLASSES:
        monkeypatch.setattr(settings_class, "model_config", config)


_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
_DATABASE_URL = "postgresql+asyncpg://ai_rss_news:secret@db:5432/ai_rss_news"


def _database_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)


def _full_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _database_env(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)


def test_settings_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _full_env(monkeypatch)
    settings = Settings()
    assert settings.bot_token == _BOT_TOKEN
    assert settings.app_env == "development"
    assert settings.log_level == "INFO"
    assert str(settings.database_url).startswith("postgresql+")


def test_settings_loads_without_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # The worker service does not interact with Telegram, so it must be able to
    # start without BOT_TOKEN.
    _database_env(monkeypatch)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    settings = Settings()
    assert settings.bot_token is None
    assert str(settings.database_url).startswith("postgresql+")


def test_main_requires_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _database_env(monkeypatch)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="BOT_TOKEN is required for the bot service"):
        main()


def test_worker_starts_without_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _database_env(monkeypatch)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with patch(
        "ai_rss_news_assistant.main.NewsWorker.run",
        new_callable=AsyncMock,
        return_value=None,
    ):
        worker()  # must not raise


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_database_url_must_be_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    _full_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./news.db")
    with pytest.raises(ValidationError):
        Settings()


def test_database_url_must_use_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    """A synchronous or non-asyncpg URL would fail only at runtime."""
    _full_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@db:5432/news")
    with pytest.raises(ValidationError, match="asyncpg"):
        Settings()


def test_blank_bot_token_is_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose passes an empty string when BOT_TOKEN is unset."""
    _database_env(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "   ")
    settings = Settings()
    assert settings.bot_token is None


def test_app_env_default_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    _full_env(monkeypatch)

    monkeypatch.setenv("APP_ENV", "Production")
    assert Settings().app_env == "production"

    monkeypatch.setenv("APP_ENV", "bad-env")
    with pytest.raises(ValidationError):
        Settings()


def test_log_level_default_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    _full_env(monkeypatch)

    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings().log_level == "DEBUG"

    monkeypatch.setenv("LOG_LEVEL", "NOT_A_LEVEL")
    with pytest.raises(ValidationError):
        Settings()


def test_llm_settings_have_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM configuration is optional so delivery works without a provider."""
    _database_env(monkeypatch)
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.llm_api_key is None
    assert settings.llm_base_url == "https://api.avalai.ir/v1"
    assert settings.llm_model
    assert settings.llm_timeout_seconds == 30


def test_llm_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    _full_env(monkeypatch)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValidationError):
        Settings()


# --- Per-service settings ---


def test_worker_settings_read_polling_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's environment variables keep their existing names."""
    _database_env(monkeypatch)
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("FETCH_FULL_TEXT", "false")
    monkeypatch.setenv("FETCH_TIMEOUT_SECONDS", "5")

    settings = WorkerSettings()

    assert settings.poll_interval_seconds == 60
    assert settings.fetch_full_text is False
    assert settings.fetch_timeout_seconds == 5


def test_worker_settings_have_defaults_without_bot_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker with no Telegram token still starts with sane defaults."""
    _database_env(monkeypatch)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    for name in (
        "POLL_INTERVAL_SECONDS",
        "FETCH_FULL_TEXT",
        "FETCH_TIMEOUT_SECONDS",
        "AGENT_INTERNAL_TOKEN",
        "AGENT_BASE_URL",
        "AGENT_PORT",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = WorkerSettings()

    assert settings.bot_token is None
    assert settings.poll_interval_seconds == 300
    assert settings.fetch_full_text is True
    assert settings.fetch_timeout_seconds == 30


def test_worker_settings_reject_non_positive_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database_env(monkeypatch)

    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    with pytest.raises(ValidationError, match="poll_interval_seconds"):
        WorkerSettings()

    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("FETCH_TIMEOUT_SECONDS", "-1")
    with pytest.raises(ValidationError, match="fetch_timeout_seconds"):
        WorkerSettings()


def test_bot_settings_default_to_the_agent_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _full_env(monkeypatch)
    for name in ("AGENT_BASE_URL", "AGENT_TIMEOUT_SECONDS", "AGENT_INTERNAL_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    settings = BotSettings()

    assert settings.agent_base_url == "http://agent:8000"
    assert settings.agent_timeout_seconds == 40
    # An unconfigured token must not be mistaken for a valid credential.
    assert settings.agent_internal_token is None


def test_bot_settings_reject_non_positive_agent_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _full_env(monkeypatch)
    monkeypatch.setenv("AGENT_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValidationError, match="agent_timeout_seconds"):
        BotSettings()


def test_blank_agent_internal_token_is_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace from Compose must not become an accepted bearer token."""
    _full_env(monkeypatch)
    monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "   ")

    assert BotSettings().agent_internal_token is None
    assert AgentSettings().agent_internal_token is None


def test_agent_settings_defaults_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent validates its own bind settings and needs no BOT_TOKEN."""
    _database_env(monkeypatch)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    for name in ("AGENT_HOST", "AGENT_PORT", "AGENT_BASE_URL", "AGENT_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    settings = AgentSettings()

    assert settings.bot_token is None
    assert settings.agent_host == "0.0.0.0"
    assert settings.agent_port == 8000

    monkeypatch.setenv("AGENT_PORT", "70000")
    with pytest.raises(ValidationError, match="agent_port"):
        AgentSettings()


def test_service_settings_only_require_the_shared_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No service fails to start because another service's setting is unset."""
    for name in (
        "BOT_TOKEN",
        "AGENT_INTERNAL_TOKEN",
        "AGENT_BASE_URL",
        "AGENT_HOST",
        "AGENT_PORT",
        "POLL_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    for settings_class in (BotSettings, WorkerSettings, AgentSettings):
        with pytest.raises(ValidationError, match="database_url"):
            settings_class()
