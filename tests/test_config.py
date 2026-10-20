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

from ai_rss_news_assistant.config import Settings
from ai_rss_news_assistant.main import main, worker


@pytest.fixture(autouse=True)
def _ignore_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not load values from the local .env file during tests.

    Tests set environment variables explicitly via monkeypatch. A pre-existing
    .env file (gitignored, not part of this repo) must not interfere.
    """
    monkeypatch.setattr(
        Settings,
        "model_config",
        SettingsConfigDict(env_file=None, env_file_encoding="utf-8", extra="ignore"),
    )


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
