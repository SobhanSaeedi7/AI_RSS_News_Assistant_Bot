"""Tests for the cross-service answer contracts and the HTTP error mapping.

No service is started and no network is used: these are the payload helpers and
the status tables both the bot and the agent rely on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from ai_rss_news_assistant.agent.errors import (
    ANSWER_STATUS_HTTP_STATUS,
    AgentAuthError,
    AgentConfigurationError,
    AgentTimeoutError,
    AgentUpstreamError,
    answer_status_for_http_status,
    answer_status_for_llm_error,
    safe_error_payload,
)
from ai_rss_news_assistant.contracts import (
    AGENT_ERROR,
    AGENT_UNAVAILABLE,
    EMPTY_QUESTION,
    INSUFFICIENT_EVIDENCE,
    AgentAnswer,
    AgentSource,
    AnswerRequest,
)

# --- payload round trips ---


def test_answer_payload_round_trip_keeps_sources() -> None:
    published_at = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    answer = AgentAnswer(
        text="پاسخ [1]",
        sources=[
            AgentSource(
                title="خبر",
                url="https://example.com/a",
                feed_title="Feed",
                published_at=published_at,
            )
        ],
        grounded=True,
        status="answered",
    )

    restored = AgentAnswer.from_payload(answer.to_payload())

    assert restored == answer
    assert restored.sources[0].published_at == published_at


def test_non_grounded_answer_uses_canonical_text() -> None:
    assert AgentAnswer.for_status("insufficient_evidence").text == INSUFFICIENT_EVIDENCE
    assert AgentAnswer.for_status("unavailable").text == AGENT_UNAVAILABLE
    assert AgentAnswer.for_status("timeout").text == AGENT_ERROR
    assert AgentAnswer.for_status("empty_question").text == EMPTY_QUESTION

    answer = AgentAnswer.for_status("unavailable")

    assert answer.grounded is False
    assert answer.sources == []


def test_answer_from_payload_rejects_unknown_status() -> None:
    payload = AgentAnswer.for_status("unavailable").to_payload()
    payload["status"] = "definitely-not-a-status"

    with pytest.raises(ValueError):
        AgentAnswer.from_payload(payload)


def test_answer_from_payload_rejects_wrong_types() -> None:
    payload = AgentAnswer.for_status("unavailable").to_payload()
    payload["grounded"] = "yes"

    with pytest.raises(TypeError):
        AgentAnswer.from_payload(payload)


def test_answer_from_payload_rejects_bad_source_timestamp() -> None:
    payload = AgentAnswer(
        text="x",
        sources=[AgentSource(title="t", url=None, published_at=None)],
        grounded=True,
    ).to_payload()
    payload["sources"][0]["published_at"] = "not-a-timestamp"

    with pytest.raises(ValueError):
        AgentAnswer.from_payload(payload)


def test_answer_request_payload_round_trip() -> None:
    user_id = uuid.uuid4()
    request = AnswerRequest(user_id=user_id, question="چه خبر؟")

    restored = AnswerRequest.from_payload(request.to_payload())

    assert restored == request
    assert restored.to_payload()["user_id"] == str(user_id)


def test_answer_request_rejects_non_uuid_user_id() -> None:
    with pytest.raises(ValueError):
        AnswerRequest.from_payload({"user_id": "not-a-uuid", "question": "سؤال"})

    with pytest.raises(TypeError):
        AnswerRequest.from_payload({"user_id": 42, "question": "سؤال"})


# --- HTTP status mapping ---


def test_answer_statuses_map_to_documented_http_codes() -> None:
    assert ANSWER_STATUS_HTTP_STATUS["answered"] == 200
    assert ANSWER_STATUS_HTTP_STATUS["insufficient_evidence"] == 200
    assert ANSWER_STATUS_HTTP_STATUS["empty_question"] == 200
    assert ANSWER_STATUS_HTTP_STATUS["unavailable"] == 503
    assert ANSWER_STATUS_HTTP_STATUS["timeout"] == 504
    assert ANSWER_STATUS_HTTP_STATUS["provider_error"] == 502


def test_every_failure_code_maps_back_to_an_answer_status() -> None:
    """A successful response is not mapped: its body carries the status."""
    for status, code in ANSWER_STATUS_HTTP_STATUS.items():
        if code == 200:
            continue
        assert answer_status_for_http_status(code) == status

    assert answer_status_for_http_status(200) == "provider_error"
    # An unexpected code is treated as an upstream failure, never as an answer.
    assert answer_status_for_http_status(418) == "provider_error"


def test_llm_classification_decides_answer_status() -> None:
    assert answer_status_for_llm_error("timeout") == "timeout"
    assert answer_status_for_llm_error("provider") == "provider_error"


def test_error_types_carry_their_http_status_and_safe_code() -> None:
    assert AgentAuthError().http_status == 401
    assert AgentConfigurationError().http_status == 503
    assert AgentUpstreamError("boom", kind="provider").http_status == 502
    assert AgentTimeoutError("slow").http_status == 504
    assert AgentTimeoutError().kind == "timeout"


def test_safe_error_payload_never_exposes_diagnostics() -> None:
    payload = safe_error_payload(AgentUpstreamError("HTTP 500 body=secret-value"))

    assert payload["error"] == "upstream_error"
    assert "secret-value" not in str(payload)
    assert set(payload) == {"error", "message"}
