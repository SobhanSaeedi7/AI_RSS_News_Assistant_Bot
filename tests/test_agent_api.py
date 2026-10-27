"""Tests for the agent's internal HTTP API.

The app is exercised in-process through an ASGI transport: no socket is opened,
no Uvicorn process is started, and the answerer is a stub, so nothing here
contacts a provider or a real database.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from ai_rss_news_assistant.agent.api import create_app
from ai_rss_news_assistant.config import AgentSettings
from ai_rss_news_assistant.contracts import AgentAnswer, AgentSource

_SECRET = "internal-token-for-tests-not-real"
_USER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _settings(*, token: str | None = _SECRET) -> AgentSettings:
    return AgentSettings(
        _env_file=None,
        agent_internal_token=token,
        database_url="postgresql+asyncpg://user:password@db:5432/news",
    )


class StubAnswerer:
    """Answerer that records calls and returns a prepared answer or error."""

    def __init__(
        self, *, answer: AgentAnswer | None = None, error: Exception | None = None
    ) -> None:
        self.answer_object = answer or AgentAnswer(text="پاسخ", grounded=True)
        self.error = error
        self.calls: list[tuple[uuid.UUID, str]] = []

    async def answer(self, user_id: uuid.UUID, question: str) -> AgentAnswer:
        self.calls.append((user_id, question))
        if self.error is not None:
            raise self.error
        return self.answer_object


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent"
    )


def _auth(token: str = _SECRET) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_health_is_alive_without_authentication() -> None:
    async with _client(create_app(settings=_settings(), answerer=StubAnswerer())) as c:
        response = await c.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_answer_requires_a_bearer_token() -> None:
    answerer = StubAnswerer()
    async with _client(create_app(settings=_settings(), answerer=answerer)) as c:
        response = await c.post(
            "/answer", json={"user_id": str(_USER_ID), "question": "خبر چیست؟"}
        )

    assert response.status_code == 401
    assert answerer.calls == []


@pytest.mark.anyio
async def test_answer_rejects_a_wrong_token() -> None:
    answerer = StubAnswerer()
    async with _client(create_app(settings=_settings(), answerer=answerer)) as c:
        response = await c.post(
            "/answer",
            json={"user_id": str(_USER_ID), "question": "خبر چیست؟"},
            headers=_auth("wrong-token"),
        )

    assert response.status_code == 401
    assert answerer.calls == []
    assert _SECRET not in response.text


@pytest.mark.anyio
async def test_unconfigured_token_refuses_every_request() -> None:
    """An unset token must never degrade into "no authentication required"."""
    answerer = StubAnswerer()
    app = create_app(settings=_settings(token=None), answerer=answerer)
    async with _client(app) as c:
        with_header = await c.post(
            "/answer",
            json={"user_id": str(_USER_ID), "question": "خبر چیست؟"},
            headers=_auth(""),
        )
        without_header = await c.post(
            "/answer", json={"user_id": str(_USER_ID), "question": "خبر چیست؟"}
        )

    assert with_header.status_code == 503
    assert without_header.status_code == 503
    assert answerer.calls == []


@pytest.mark.anyio
async def test_grounded_answer_returns_text_and_sources() -> None:
    answerer = StubAnswerer(
        answer=AgentAnswer(
            text="خلاصهٔ پاسخ [1]",
            sources=[
                AgentSource(
                    title="عنوان خبر", url="https://example.com/article/1"
                )
            ],
            grounded=True,
        )
    )
    async with _client(create_app(settings=_settings(), answerer=answerer)) as c:
        response = await c.post(
            "/answer",
            json={"user_id": str(_USER_ID), "question": "خبرهای فناوری"},
            headers=_auth(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "answered"
    assert payload["grounded"] is True
    assert payload["text"] == "خلاصهٔ پاسخ [1]"
    assert payload["sources"][0]["url"] == "https://example.com/article/1"
    # The internal user id reaches the answerer unchanged, which is what keeps
    # retrieval scoped to that user.
    assert answerer.calls == [(_USER_ID, "خبرهای فناوری")]


@pytest.mark.anyio
async def test_insufficient_evidence_is_a_successful_answer() -> None:
    answerer = StubAnswerer(answer=AgentAnswer.for_status("insufficient_evidence"))
    async with _client(create_app(settings=_settings(), answerer=answerer)) as c:
        response = await c.post(
            "/answer",
            json={"user_id": str(_USER_ID), "question": "سؤال بی‌ربط"},
            headers=_auth(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "insufficient_evidence"
    assert payload["grounded"] is False
    assert payload["sources"] == []
    assert payload["text"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        ({}, 422),
        ({"user_id": "not-a-uuid", "question": "خبر چیست؟"}, 422),
        ({"user_id": str(_USER_ID), "question": "x" * 501}, 422),
        ({"user_id": str(_USER_ID)}, 422),
        (
            {"user_id": str(_USER_ID), "question": "خبر چیست؟", "extra": "nope"},
            422,
        ),
    ],
)
async def test_invalid_requests_are_rejected_before_answering(
    body: dict[str, object], expected_status: int
) -> None:
    answerer = StubAnswerer()
    async with _client(create_app(settings=_settings(), answerer=answerer)) as c:
        response = await c.post("/answer", json=body, headers=_auth())

    assert response.status_code == expected_status
    assert answerer.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer_status", "expected_http_status"),
    [
        ("timeout", 504),
        ("provider_error", 502),
        ("unavailable", 503),
    ],
)
async def test_answer_status_maps_to_the_documented_http_status(
    answer_status: str, expected_http_status: int
) -> None:
    answerer = StubAnswerer(answer=AgentAnswer.for_status(answer_status))
    async with _client(create_app(settings=_settings(), answerer=answerer)) as c:
        response = await c.post(
            "/answer",
            json={"user_id": str(_USER_ID), "question": "خبر چیست؟"},
            headers=_auth(),
        )

    assert response.status_code == expected_http_status
    assert response.json()["status"] == answer_status
    # The body still carries the canonical text so the bot can render it.
    assert response.json()["text"]


@pytest.mark.anyio
async def test_unexpected_failure_returns_a_sanitized_500() -> None:
    answerer = StubAnswerer(error=RuntimeError("boom: internal detail"))
    async with _client(create_app(settings=_settings(), answerer=answerer)) as c:
        response = await c.post(
            "/answer",
            json={"user_id": str(_USER_ID), "question": "خبر چیست؟"},
            headers=_auth(),
        )

    assert response.status_code == 500
    payload = response.json()
    assert payload["status"] == "provider_error"
    assert payload["sources"] == []
    # No exception text, path, or traceback may reach the caller.
    assert "boom" not in response.text
    assert "Traceback" not in response.text
    assert _SECRET not in response.text
