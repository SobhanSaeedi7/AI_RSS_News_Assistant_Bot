"""Tests for the bot's HTTP client for the agent service.

Every response is produced by an in-process ``httpx.MockTransport``: no socket is
opened, no Telegram call is made, and no provider is contacted. The tests pin the
status mapping, the request the agent receives, and that a failing agent yields a
renderable answer instead of an exception.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from ai_rss_news_assistant.bot.agent_client import AgentHTTPClient
from ai_rss_news_assistant.contracts import AGENT_ERROR, AGENT_UNAVAILABLE

_TOKEN = "internal-token-for-tests-not-real"
_BASE_URL = "http://agent:8000"
_USER_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


class RecordingTransport(httpx.MockTransport):
    """Mock transport that records the requests it receives.

    The client calls the transport asynchronously, so requests are recorded on
    the async entry point only.
    """

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await super().handle_async_request(request)


def _client(handler, **kwargs) -> AgentHTTPClient:
    return AgentHTTPClient(
        base_url=_BASE_URL,
        token=_TOKEN,
        timeout_seconds=40,
        transport=RecordingTransport(handler),
        **kwargs,
    )


def _json_response(status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode(),
    )


def _answer_payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "answered",
        "text": "پاسخ [1]",
        "grounded": True,
        "sources": [
            {
                "title": "عنوان خبر",
                "url": "https://example.com/article/1",
                "feed_title": "فید نمونه",
                "published_at": "2026-01-02T03:04:05+00:00",
            }
        ],
    }
    payload.update(overrides)
    return payload


@pytest.mark.anyio
async def test_grounded_answer_is_parsed_with_its_sources() -> None:
    transport = RecordingTransport(
        lambda request: _json_response(200, _answer_payload())
    )
    client = AgentHTTPClient(
        base_url=_BASE_URL, token=_TOKEN, timeout_seconds=40, transport=transport
    )

    answer = await client.answer(_USER_ID, "خبرهای فناوری")

    assert answer.status == "answered"
    assert answer.grounded is True
    assert answer.text == "پاسخ [1]"
    assert [source.url for source in answer.sources] == [
        "https://example.com/article/1"
    ]
    assert answer.sources[0].feed_title == "فید نمونه"
    assert answer.sources[0].published_at is not None


@pytest.mark.anyio
async def test_request_carries_bearer_token_and_user_id() -> None:
    """The user id travels in the body and the token only in the header."""
    transport = RecordingTransport(
        lambda request: _json_response(200, _answer_payload())
    )
    client = AgentHTTPClient(
        base_url=_BASE_URL, token=_TOKEN, timeout_seconds=40, transport=transport
    )

    await client.answer(_USER_ID, "سؤال من")

    request = transport.requests[0]
    assert str(request.url) == f"{_BASE_URL}/answer"
    assert request.headers["authorization"] == f"Bearer {_TOKEN}"
    body = json.loads(request.content)
    assert body == {"user_id": str(_USER_ID), "question": "سؤال من"}


@pytest.mark.anyio
async def test_insufficient_evidence_is_not_grounded() -> None:
    handler = lambda request: _json_response(
        200,
        _answer_payload(
            status="insufficient_evidence",
            text="چیزی پیدا نشد",
            grounded=False,
            sources=[],
        ),
    )

    answer = await _client(handler).answer(_USER_ID, "سؤال")

    assert answer.status == "insufficient_evidence"
    assert answer.grounded is False
    assert answer.sources == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status_code", "expected_status"),
    [
        (401, "unavailable"),
        (403, "unavailable"),
        (422, "provider_error"),
        (502, "provider_error"),
        (503, "unavailable"),
        (504, "timeout"),
        (500, "provider_error"),
    ],
)
async def test_http_status_codes_map_to_answer_statuses(
    status_code: int, expected_status: str
) -> None:
    handler = lambda request: _json_response(status_code, {"detail": "internal"})

    answer = await _client(handler).answer(_USER_ID, "سؤال")

    assert answer.status == expected_status
    assert answer.grounded is False
    assert answer.sources == []


@pytest.mark.anyio
async def test_timeout_is_reported_as_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    answer = await _client(handler).answer(_USER_ID, "سؤال")

    assert answer.status == "timeout"
    assert answer.text == AGENT_ERROR


@pytest.mark.anyio
async def test_unreachable_agent_is_reported_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    answer = await _client(handler).answer(_USER_ID, "سؤال")

    assert answer.status == "unavailable"
    assert answer.text == AGENT_UNAVAILABLE


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(status_code=200, content=b"not json"),
        httpx.Response(
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps({"status": "who knows"}).encode(),
        ),
        httpx.Response(
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {"status": "answered", "sources": "not a list"}
            ).encode(),
        ),
    ],
)
async def test_malformed_success_bodies_never_leak_into_the_answer(
    response: httpx.Response,
) -> None:
    answer = await _client(lambda request: response).answer(_USER_ID, "سؤال")

    assert answer.status == "provider_error"
    assert answer.grounded is False
    assert "who knows" not in answer.text


@pytest.mark.anyio
async def test_error_body_text_is_not_used_in_the_answer() -> None:
    """Only the contract's canonical texts may reach Telegram."""
    handler = lambda request: _json_response(
        502, _answer_payload(text="leaked upstream detail")
    )

    answer = await _client(handler).answer(_USER_ID, "سؤال")

    assert answer.status == "provider_error"
    assert answer.text == AGENT_ERROR
    assert "leaked" not in answer.text
