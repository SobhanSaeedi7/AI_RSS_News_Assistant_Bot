"""Tests for the OpenAI-compatible client's HTTP and transport error handling.

No real HTTP request is made: ``urllib.request.urlopen`` is patched and the
provider response is supplied through a fake ``HTTPError`` body.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from ai_rss_news_assistant.llm import (
    MAX_ERROR_MESSAGE_CHARS,
    LLMError,
    OpenAICompatibleClient,
    _http_error_message,
)

_API_KEY = "sk-test-super-secret-key-123456"


def _http_error(status: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.example.com/v1/chat/completions",
        status,
        "Too Many Requests",
        {},
        io.BytesIO(body.encode("utf-8")),
    )


def _client(timeout_seconds: int = 30) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        api_key=_API_KEY,
        base_url="https://api.example.com/v1",
        model="gpt-4o-mini",
        timeout_seconds=timeout_seconds,
    )


def test_http_429_exposes_structured_provider_error() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "Rate limit reached for gpt-4o-mini.",
                "type": "requests",
                "param": None,
                "code": "rate_limit_exceeded",
            }
        }
    )

    message = _http_error_message(_http_error(429, body), _API_KEY)

    assert message.startswith("LLM request failed with HTTP 429")
    assert "type=requests" in message
    assert "code=rate_limit_exceeded" in message
    assert "message=Rate limit reached for gpt-4o-mini." in message


def test_http_429_accepts_top_level_error_fields() -> None:
    body = json.dumps({"message": "Slow down.", "type": "rate_limit", "code": 429})

    message = _http_error_message(_http_error(429, body), _API_KEY)

    assert "type=rate_limit" in message
    assert "code=429" in message
    assert "message=Slow down." in message


def test_http_429_redacts_api_key_and_authorization_values() -> None:
    body = json.dumps(
        {
            "error": {
                "message": (
                    f"Invalid key {_API_KEY}; "
                    "Authorization: Bearer sk-another-secret-token"
                ),
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        }
    )

    message = _http_error_message(_http_error(429, body), _API_KEY)

    assert _API_KEY not in message
    assert "sk-another-secret-token" not in message
    assert "Bearer sk-" not in message
    assert "[redacted]" in message


def test_http_429_non_json_body_is_not_logged() -> None:
    body = "<html>429 Too Many Requests: <secret>internal</secret></html>"

    message = _http_error_message(_http_error(429, body), _API_KEY)

    assert message == "LLM request failed with HTTP 429"
    assert "internal" not in message


def test_http_429_unrecognised_json_body_is_not_logged() -> None:
    body = json.dumps({"error": {"unexpected": "sensitive-value"}})

    message = _http_error_message(_http_error(429, body), _API_KEY)

    assert message == "LLM request failed with HTTP 429"
    assert "sensitive-value" not in message


def test_http_429_truncates_long_provider_message() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "x" * 1000,
                "type": "requests",
                "code": "rate_limit_exceeded",
            }
        }
    )

    message = _http_error_message(_http_error(429, body), _API_KEY)

    assert "x" * (MAX_ERROR_MESSAGE_CHARS + 1) not in message
    assert len(message) < MAX_ERROR_MESSAGE_CHARS + 200


def test_request_converts_http_429_into_detailed_llm_error() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "Rate limit reached.",
                "type": "requests",
                "code": "rate_limit_exceeded",
            }
        }
    )
    client = OpenAICompatibleClient(
        api_key=_API_KEY,
        base_url="https://api.example.com/v1",
        model="gpt-4o-mini",
    )

    with (
        patch(
            "ai_rss_news_assistant.llm.urllib.request.urlopen",
            side_effect=_http_error(429, body),
        ),
        pytest.raises(LLMError) as excinfo,
    ):
        client._request(b"{}")

    error = str(excinfo.value)
    assert "HTTP 429" in error
    assert "type=requests" in error
    assert "code=rate_limit_exceeded" in error
    assert "message=Rate limit reached." in error
    assert _API_KEY not in error
    # A rejected request is a provider failure, not a timeout.
    assert excinfo.value.kind == "provider"


# --- Error classification (kind) ---


def test_llm_error_defaults_to_provider_kind() -> None:
    """Existing call sites construct LLMError with a message only."""
    error = LLMError("LLM request failed")

    assert error.kind == "provider"
    assert str(error) == "LLM request failed"


def test_request_classifies_timeout() -> None:
    """A timed-out request is reported as kind "timeout"."""
    client = _client(timeout_seconds=7)

    with (
        patch(
            "ai_rss_news_assistant.llm.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ),
        pytest.raises(LLMError) as excinfo,
    ):
        client._request(b"{}")

    assert excinfo.value.kind == "timeout"
    assert "timed out" in str(excinfo.value)
    assert _API_KEY not in str(excinfo.value)


def test_request_classifies_url_error_wrapping_timeout() -> None:
    """urllib wraps a send timeout in URLError; it is still kind "timeout"."""
    client = _client()

    with (
        patch(
            "ai_rss_news_assistant.llm.urllib.request.urlopen",
            side_effect=urllib.error.URLError(TimeoutError("timed out")),
        ),
        pytest.raises(LLMError) as excinfo,
    ):
        client._request(b"{}")

    assert excinfo.value.kind == "timeout"


def test_request_classifies_connection_failure_as_provider() -> None:
    """A transport failure that is not a timeout stays kind "provider"."""
    client = _client()

    with (
        patch(
            "ai_rss_news_assistant.llm.urllib.request.urlopen",
            side_effect=urllib.error.URLError(OSError("connection refused")),
        ),
        pytest.raises(LLMError) as excinfo,
    ):
        client._request(b"{}")

    assert excinfo.value.kind == "provider"
    assert str(excinfo.value) == "LLM request failed"


def test_request_classifies_unusable_response_as_provider() -> None:
    """A response the client cannot read is a provider failure."""
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b"not a chat completion"

    with (
        patch(
            "ai_rss_news_assistant.llm.urllib.request.urlopen",
            return_value=response,
        ),
        pytest.raises(LLMError) as excinfo,
    ):
        _client()._request(b"{}")

    assert excinfo.value.kind == "provider"
    assert str(excinfo.value) == "LLM response was not understood"
