"""Tests for the LLM-backed summarizer and its error boundary.

Summarization wraps provider failures in :class:`SummarizationError`. These
tests pin that the ``timeout``/``provider`` classification survives that wrapper
and that the message is passed through unchanged, because callers must be able
to distinguish a slow provider from a failing one without parsing text.

No network, provider, or Telegram call is made: the chat client is a stub.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from ai_rss_news_assistant.llm import LLMError
from ai_rss_news_assistant.worker.summarization import (
    LLMSummarizer,
    SummarizationError,
)


class StubClient:
    """Chat client that either raises a prepared error or returns fixed text."""

    def __init__(self, *, error: LLMError | None = None, reply: str = "") -> None:
        self.error = error
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int = 400,
        temperature: float = 0.2,
    ) -> str:
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return self.reply


def _summarizer(client: StubClient) -> LLMSummarizer:
    return LLMSummarizer(
        api_key="test-key-not-real",
        base_url="https://provider.invalid/v1",
        model="test-model",
        client=client,
    )


@pytest.mark.anyio
async def test_timeout_classification_survives_the_wrapper() -> None:
    """A provider timeout is still identifiable as a timeout after wrapping."""
    client = StubClient(
        error=LLMError("LLM request timed out after 30s", kind="timeout")
    )

    with pytest.raises(SummarizationError) as caught:
        await _summarizer(client).summarize(title="Title", content="Article body")

    assert caught.value.kind == "timeout"
    assert str(caught.value) == "LLM request timed out after 30s"
    # Callers that catch LLMError keep working: nothing about the hierarchy moved.
    assert isinstance(caught.value, LLMError)


@pytest.mark.anyio
async def test_provider_classification_survives_the_wrapper() -> None:
    """A generic provider failure stays classified as a provider failure."""
    client = StubClient(error=LLMError("LLM request failed with HTTP 503"))

    with pytest.raises(SummarizationError) as caught:
        await _summarizer(client).summarize(title="Title", content="Article body")

    assert caught.value.kind == "provider"
    assert str(caught.value) == "LLM request failed with HTTP 503"


def test_summarization_error_keeps_the_default_classification() -> None:
    """Existing construction sites that pass only a message still work."""
    error = SummarizationError("boom")

    assert error.kind == "provider"
    assert str(error) == "boom"


@pytest.mark.anyio
async def test_successful_summary_is_not_wrapped() -> None:
    client = StubClient(reply="خلاصهٔ خبر")

    summary = await _summarizer(client).summarize(title="Title", content="Body")

    assert summary == "خلاصهٔ خبر"
    assert len(client.calls) == 1


@pytest.mark.anyio
async def test_empty_content_never_calls_the_provider() -> None:
    client = StubClient(reply="should not be used")

    summary = await _summarizer(client).summarize(title="Title", content=None)

    assert summary == ""
    assert client.calls == []
