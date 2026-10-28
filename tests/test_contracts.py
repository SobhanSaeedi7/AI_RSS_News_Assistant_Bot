"""Tests for the cross-service answer contracts and the HTTP error mapping.

No service is started and no network is used: these are the payload helpers and
the status tables both the bot and the agent rely on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from ai_rss_news_assistant.contracts import (
    AGENT_ERROR,
    AGENT_UNAVAILABLE,
    ANSWER_STATUS_HTTP_STATUS,
    EMPTY_QUESTION,
    FAILURE_HTTP_STATUSES,
    INSUFFICIENT_EVIDENCE,
    AgentAnswer,
    AgentSource,
    AnswerRequest,
    answer_status_for_http_status,
    answer_status_for_llm_error,
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


def test_every_answer_status_has_an_http_mapping() -> None:
    """No answer status may reach the HTTP layer without a documented code."""
    assert set(ANSWER_STATUS_HTTP_STATUS) == {
        "answered",
        "insufficient_evidence",
        "empty_question",
        "unavailable",
        "timeout",
        "provider_error",
    }
    # Both answer statuses that are not failures must be 200: the body says why.
    assert ANSWER_STATUS_HTTP_STATUS["insufficient_evidence"] == 200
    assert ANSWER_STATUS_HTTP_STATUS["empty_question"] == 200


def test_failure_codes_cover_every_documented_error() -> None:
    """The bot must be able to map every documented agent failure code."""
    for code in (401, 403, 422, 500, 502, 503, 504):
        assert code in FAILURE_HTTP_STATUSES
        assert answer_status_for_http_status(code) in ANSWER_STATUS_HTTP_STATUS


def test_services_do_not_keep_private_status_tables() -> None:
    """One mapping lives in contracts; neither side may fork its own copy."""
    import inspect

    from ai_rss_news_assistant.agent import api
    from ai_rss_news_assistant.bot import agent_client

    for module in (api, agent_client):
        source = inspect.getsource(module)
        assert "_STATUS_BY_HTTP_CODE" not in source
        assert "_STATUS_BY_ANSWER_STATUS" not in source

    assert "ANSWER_STATUS_HTTP_STATUS" in inspect.getsource(api)
    assert "answer_status_for_http_status" in inspect.getsource(agent_client)


def test_canonical_failure_texts_never_carry_diagnostics() -> None:
    """Every non-answer status must render a fixed, user-facing message."""
    for status in (
        "insufficient_evidence",
        "empty_question",
        "unavailable",
        "timeout",
        "provider_error",
    ):
        text = AgentAnswer.for_status(status).text
        assert text
        assert "Traceback" not in text
        assert "HTTP" not in text
        assert text != AGENT_UNAVAILABLE or status == "unavailable"


# --- citation numbering -----------------------------------------------------


def test_source_payload_round_trip_keeps_the_citation_index() -> None:
    source = AgentSource(
        title="خبر",
        url="https://example.com/a",
        citation_index=2,
    )

    restored = AgentSource.from_payload(source.to_payload())

    assert restored == source
    assert restored.citation_index == 2


def test_source_payload_without_a_citation_index_still_parses() -> None:
    """An older agent payload has no citation_index; it must remain readable."""
    payload = AgentSource(title="خبر", url=None).to_payload()
    payload.pop("citation_index")

    restored = AgentSource.from_payload(payload)

    assert restored.citation_index is None


@pytest.mark.parametrize("bad_index", [0, -1, True, "1", 1.5])
def test_source_payload_rejects_an_invalid_citation_index(bad_index) -> None:
    payload = AgentSource(title="خبر", url=None).to_payload()
    payload["citation_index"] = bad_index

    with pytest.raises(TypeError):
        AgentSource.from_payload(payload)
