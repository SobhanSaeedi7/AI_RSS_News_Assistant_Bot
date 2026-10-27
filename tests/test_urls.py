"""Tests for the shared safe URL helpers.

No network access: the helpers only parse and validate strings.
"""

from __future__ import annotations

import pytest

from ai_rss_news_assistant.urls import MAX_URL_LENGTH, parse_http_url, safe_url

# --- safe_url: what may be shown as a clickable link ---


def test_safe_url_rejects_non_http_schemes() -> None:
    assert safe_url("https://example.com/a") == "https://example.com/a"
    assert safe_url("http://example.com/a") == "http://example.com/a"
    assert safe_url("javascript:alert(1)") is None
    assert safe_url("file:///etc/passwd") is None
    assert safe_url(None) is None


def test_safe_url_rejects_malformed_ipv6() -> None:
    assert safe_url("http://[::1") is None


def test_safe_url_rejects_unsafe_characters_and_credentials() -> None:
    assert safe_url("https://example.com/a b") is None
    assert safe_url("https://example.com/a\nb") is None
    assert safe_url("https://example.com/a\tb") is None
    assert safe_url("https://example.com/\x00") is None
    assert safe_url("https://user:pass@example.com/") is None
    assert safe_url("https://example.com/" + "a" * 2100) is None


def test_safe_url_length_limit_is_a_shared_constant() -> None:
    within_limit = "https://example.com/" + "a" * (MAX_URL_LENGTH - 20)

    assert len(within_limit) == MAX_URL_LENGTH
    assert safe_url(within_limit) == within_limit
    assert safe_url(within_limit + "b") is None


# --- parse_http_url: which feed URLs may be stored ---


def test_parse_http_url_accepts_http_and_https() -> None:
    assert parse_http_url("https://example.com/feed.xml").hostname == "example.com"
    assert parse_http_url("http://example.com/feed.xml").scheme == "http"


def test_parse_http_url_rejects_unsupported_scheme() -> None:
    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        parse_http_url("ftp://example.com/feed.xml")


def test_parse_http_url_requires_a_hostname() -> None:
    with pytest.raises(ValueError, match="hostname"):
        parse_http_url("https:///feed.xml")


def test_parse_http_url_rejects_embedded_credentials() -> None:
    """Credentials must never be stored or written to logs."""
    with pytest.raises(ValueError, match="credentials"):
        parse_http_url("https://user:password@example.com/feed.xml")


def test_parse_http_url_raises_value_error_for_malformed_ipv6() -> None:
    """Callers only need one except clause for every bad input."""
    with pytest.raises(ValueError):
        parse_http_url("http://[::1")
