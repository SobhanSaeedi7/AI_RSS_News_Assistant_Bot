"""Tests for application logging configuration."""

from __future__ import annotations

import logging

from ai_rss_news_assistant.logging import setup_logging


def test_setup_logging_suppresses_httpx_request_urls(monkeypatch) -> None:
    """httpx request lines (which contain bot tokens) are not emitted.

    Application loggers must keep working, including errors.
    """
    # Avoid reconfiguring the real root logger / pytest capture handlers.
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)

    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    app_logger = logging.getLogger("ai_rss_news_assistant.test_logging")

    original_root_level = root.level
    original_httpx_level = httpx_logger.level
    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    collector = Collector()
    root.addHandler(collector)
    root.setLevel(logging.INFO)
    try:
        setup_logging("DEBUG")

        assert httpx_logger.getEffectiveLevel() == logging.WARNING

        # A Telegram request URL must not produce a log record.
        httpx_logger.info(
            "HTTP Request: POST https://api.telegram.org/bot123456:fake-token/getMe"
        )
        assert records == []

        # Application logs (including errors) still pass through.
        app_logger.info("application message")
        app_logger.error("application error")
        messages = [record.getMessage() for record in records]
        assert "application message" in messages
        assert "application error" in messages
    finally:
        root.removeHandler(collector)
        root.setLevel(original_root_level)
        httpx_logger.setLevel(original_httpx_level)
