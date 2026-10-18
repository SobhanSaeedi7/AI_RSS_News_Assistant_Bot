"""Tests for the application configuration system.

These tests exercise the foundational configuration behavior: required secrets
fail fast, optional fields have sensible defaults, and validators reject
invalid values. No network, Telegram, database, or LLM calls are made.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_rss_news_assistant.config import Settings

_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
_DATABASE_URL = "postgresql+asyncpg://ai_rss_news:secret@db:5432/ai_rss_news"


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)


def test_settings_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    settings = Settings()
    assert settings.bot_token == _BOT_TOKEN
    assert settings.app_env == "development"
    assert settings.log_level == "INFO"
    assert str(settings.database_url).startswith("postgresql+")


def test_bot_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)
    with pytest.raises(ValidationError):
        Settings()


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_database_url_must_be_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./news.db")
    with pytest.raises(ValidationError):
        Settings()


def test_app_env_default_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)

    monkeypatch.setenv("APP_ENV", "Production")
    assert Settings().app_env == "production"

    monkeypatch.setenv("APP_ENV", "bad-env")
    with pytest.raises(ValidationError):
        Settings()


def test_log_level_default_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)

    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings().log_level == "DEBUG"

    monkeypatch.setenv("LOG_LEVEL", "NOT_A_LEVEL")
    with pytest.raises(ValidationError):
        Settings()
