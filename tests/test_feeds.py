"""Tests for the shared feed probe.

Every test patches the HTTP layer: nothing here resolves a feed over the
network, and nothing depends on a live RSS site.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from ai_rss_news_assistant.feeds import (
    MAX_TITLE_CHARS,
    BlockedDestinationError,
    FeedNotUsableError,
    FeedProbeStatus,
    classify_parsed_feed,
    probe_feed,
    validate_feed_url,
)

FEED_URL = "https://example.com/feed.xml"


def _parsed(
    *,
    version: str = "rss20",
    title: str | None = "Example Feed",
    entries: int = 1,
    bozo: bool = False,
) -> dict:
    """Build the shape feedparser returns, without parsing anything."""
    return {
        "version": version,
        "bozo": bozo,
        "feed": {"title": title} if title is not None else {},
        "entries": [{"link": f"https://example.com/{i}"} for i in range(entries)],
    }


def _response(content: bytes = b"<rss/>") -> MagicMock:
    response = MagicMock()
    response.geturl.return_value = FEED_URL
    response.read.return_value = content
    response.__enter__.return_value = response
    response.__exit__.return_value = None
    return response


# --- classification of an already-parsed feed ------------------------------


def test_valid_feed_with_entries_is_accepted() -> None:
    result = classify_parsed_feed(_parsed(entries=3))

    assert result.status is FeedProbeStatus.VALID
    assert result.is_usable is True
    assert result.title == "Example Feed"
    assert result.entries == 3


def test_atom_feed_with_no_entries_is_accepted_but_empty() -> None:
    """A brand-new feed with no items yet is valid, not an error."""
    result = classify_parsed_feed(_parsed(version="atom10", entries=0))

    assert result.status is FeedProbeStatus.EMPTY
    assert result.is_usable is True
    assert result.title == "Example Feed"


def test_html_page_is_not_a_feed() -> None:
    """feedparser reports an HTML page as no version and no entries."""
    result = classify_parsed_feed(_parsed(version="", title=None, entries=0))

    assert result.status is FeedProbeStatus.NOT_A_FEED
    assert result.is_usable is False


def test_recoverable_parser_warning_still_accepts_the_feed() -> None:
    """bozo is a warning, not a verdict: real feeds set it and still work."""
    result = classify_parsed_feed(_parsed(entries=2, bozo=True))

    assert result.status is FeedProbeStatus.VALID
    assert result.entries == 2


def test_bozo_document_without_a_feed_shape_is_rejected() -> None:
    result = classify_parsed_feed(_parsed(version="", title=None, entries=0, bozo=True))

    assert result.status is FeedProbeStatus.NOT_A_FEED


def test_feed_title_is_cleaned_and_bounded() -> None:
    result = classify_parsed_feed(_parsed(title="  My   Feed\n "))

    assert result.title == "My Feed"

    long_title = classify_parsed_feed(_parsed(title="x" * 500))
    assert long_title.title is not None
    assert len(long_title.title) == MAX_TITLE_CHARS


def test_non_dict_parsed_input_is_not_a_feed() -> None:
    assert classify_parsed_feed(None).status is FeedProbeStatus.NOT_A_FEED
    assert classify_parsed_feed([]).status is FeedProbeStatus.NOT_A_FEED


# --- probing a URL ---------------------------------------------------------


def test_probe_returns_the_feed_title_for_a_valid_feed() -> None:
    with (
        patch("ai_rss_news_assistant.feeds._open_url", return_value=_response()),
        patch(
            "ai_rss_news_assistant.feeds.feedparser.parse",
            return_value=_parsed(entries=1),
        ),
    ):
        result = probe_feed(FEED_URL)

    assert result.status is FeedProbeStatus.VALID
    assert result.title == "Example Feed"


def test_probe_rejects_a_plain_html_page() -> None:
    with (
        patch("ai_rss_news_assistant.feeds._open_url", return_value=_response()),
        patch(
            "ai_rss_news_assistant.feeds.feedparser.parse",
            return_value=_parsed(version="", title=None, entries=0),
        ),
    ):
        result = probe_feed("https://example.com/")

    assert result.status is FeedProbeStatus.NOT_A_FEED


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError(FEED_URL, 404, "Not Found", {}, None),
        urllib.error.HTTPError(FEED_URL, 500, "Server Error", {}, None),
        urllib.error.URLError("dns failure"),
        TimeoutError("timed out"),
        OSError("connection refused"),
    ],
    ids=["404", "500", "dns", "timeout", "connection"],
)
def test_probe_reports_unreachable_without_raising(failure: BaseException) -> None:
    with patch("ai_rss_news_assistant.feeds._open_url", side_effect=failure):
        result = probe_feed(FEED_URL, timeout=5)

    assert result.status is FeedProbeStatus.UNREACHABLE
    assert result.is_usable is False


def test_probe_reports_a_parser_crash_as_not_a_feed() -> None:
    with (
        patch("ai_rss_news_assistant.feeds._open_url", return_value=_response()),
        patch(
            "ai_rss_news_assistant.feeds.feedparser.parse",
            side_effect=ValueError("broken"),
        ),
    ):
        result = probe_feed(FEED_URL)

    assert result.status is FeedProbeStatus.NOT_A_FEED


def test_probe_blocks_private_destinations() -> None:
    """SSRF protection applies before any request is attempted."""
    with patch("ai_rss_news_assistant.feeds._open_url") as mock_open:
        result = probe_feed("http://127.0.0.1/feed.xml")

    assert result.status is FeedProbeStatus.BLOCKED
    mock_open.assert_not_called()


def test_probe_reports_an_invalid_url() -> None:
    assert probe_feed("ftp://example.com/feed.xml").status is FeedProbeStatus.INVALID
    assert probe_feed("not a url").status is FeedProbeStatus.INVALID


def test_probe_reports_a_blocked_redirect() -> None:
    """A redirect into the internal network is caught during the fetch."""
    with patch(
        "ai_rss_news_assistant.feeds._open_url",
        side_effect=BlockedDestinationError("redirect to private address"),
    ):
        result = probe_feed(FEED_URL)

    assert result.status is FeedProbeStatus.BLOCKED


def test_validate_feed_url_rejects_credentials() -> None:
    with pytest.raises(ValueError, match="credentials"):
        validate_feed_url("https://user:password@example.com/feed.xml")


def test_feed_not_usable_error_is_a_value_error() -> None:
    """Ingestion classifies it through the ValueError branch."""
    assert issubclass(FeedNotUsableError, ValueError)
