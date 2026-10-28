"""Internal HTTP API of the agent service.

The bot is the only client: it calls these endpoints over the private Compose
network with the bearer token from ``AGENT_INTERNAL_TOKEN``. The service is
bound to the container network and is never published to the host.

Endpoints
---------

``GET /health``
    Liveness only. It reads no data and is intentionally unauthenticated.

``POST /answer``
    Answers one question for one internal user id. The id is the bot's own user
    id, so retrieval stays scoped to that user; the caller cannot ask for
    anyone else's news because there is no other way to name a user.

Status codes
------------

* ``200`` -- a rendered answer, including the ``insufficient_evidence`` and
  ``empty_question`` outcomes, which are answers rather than HTTP failures.
* ``401`` -- missing or invalid bearer token.
* ``422`` -- body fails validation (bad UUID, question over 500 characters,
  unknown fields); produced by the framework's own validation.
* ``502`` -- the provider failed for the agent (``provider_error``).
* ``503`` -- the agent is not configured (no bearer token), or the answer
  service reports that answering is unavailable.
* ``504`` -- the provider timed out (``timeout``).
* ``500`` -- unexpected internal failure.

Every non-200 response body is the same JSON shape as a successful answer, so a
client can always read a machine-readable status and a canonical, already
translated message. Bodies never contain exception text, request data, or
credentials: the fixed texts come from
:mod:`ai_rss_news_assistant.contracts`, and the status comes from the
:class:`~ai_rss_news_assistant.llm.LLMError` classification rather than from
parsing an exception message.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from ai_rss_news_assistant.agent.answer import NewsAgent
from ai_rss_news_assistant.config import AgentSettings
from ai_rss_news_assistant.contracts import (
    ANSWER_STATUS_HTTP_STATUS,
    MAX_QUESTION_CHARS,
    AgentAnswer,
)

logger = logging.getLogger(__name__)

# The failure statuses themselves come from the shared contract table; these
# are the codes this module raises or returns directly.
HTTP_200_OK = status.HTTP_200_OK
HTTP_401_UNAUTHORIZED = status.HTTP_401_UNAUTHORIZED
HTTP_500_INTERNAL_SERVER_ERROR = status.HTTP_500_INTERNAL_SERVER_ERROR
HTTP_503_SERVICE_UNAVAILABLE = status.HTTP_503_SERVICE_UNAVAILABLE

_bearer_scheme = HTTPBearer(auto_error=False)


class AnswerRequestBody(BaseModel):
    """Validated body of ``POST /answer``.

    Extra fields are rejected so a caller cannot smuggle a second user id or a
    different retrieval scope past validation.
    """

    model_config = ConfigDict(extra="forbid")

    # Typed as a UUID so a malformed id is rejected with 422 before it can reach
    # a query, and so the answerer always receives a real UUID.
    user_id: uuid.UUID = Field(
        description="Internal id of the user asking the question"
    )
    question: str = Field(
        max_length=MAX_QUESTION_CHARS,
        description=f"Question text, at most {MAX_QUESTION_CHARS} characters",
    )


def _payload_response(status_code: int, answer: AgentAnswer) -> JSONResponse:
    """Return an answer payload with the given HTTP status code."""
    return JSONResponse(status_code=status_code, content=answer.to_payload())


def _current_settings(request: Request) -> AgentSettings:
    """Read the settings the application was created with."""
    settings: AgentSettings = request.app.state.settings
    return settings


def _current_answerer(request: Request) -> NewsAgent:
    """Read the answer service the application was created with."""
    answerer: NewsAgent = request.app.state.answerer
    return answerer


_Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)]
_Settings = Annotated[AgentSettings, Depends(_current_settings)]
_Answerer = Annotated[NewsAgent, Depends(_current_answerer)]


async def require_internal_token(credentials: _Credentials, settings: _Settings) -> str:
    """Require the shared bearer token, or refuse to serve anything.

    Returns the verified token so the dependency has a concrete type; endpoints
    never use the value itself.
    """
    expected = settings.agent_internal_token
    if not expected:
        # An unset token must never mean "no authentication required".
        logger.error("AGENT_INTERNAL_TOKEN is not configured; refusing requests")
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent is not configured",
        )
    provided = credentials.credentials if credentials is not None else ""
    if not provided or not secrets.compare_digest(provided, expected):
        logger.warning("Rejected an agent request with a missing or invalid token")
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="missing or invalid internal token",
        )
    return expected


_Authorized = Annotated[str, Depends(require_internal_token)]

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe: the process is up and serving."""
    return {"status": "ok"}


@router.post("/answer")
async def answer(
    body: AnswerRequestBody,
    _token: _Authorized,
    answerer: _Answerer,
) -> JSONResponse:
    """Answer one question from the requesting user's stored news."""
    try:
        answer_object = await answerer.answer(body.user_id, body.question)
    except Exception:
        # Traces go to the container logs, never to the response.
        logger.exception("Agent failed to answer a question")
        return _payload_response(
            HTTP_500_INTERNAL_SERVER_ERROR,
            AgentAnswer.for_status("provider_error"),
        )

    # The mapping is the shared contract table, not a local copy, so the bot's
    # reverse mapping cannot disagree with what this endpoint returns.
    status_code = ANSWER_STATUS_HTTP_STATUS.get(answer_object.status, HTTP_200_OK)
    return _payload_response(status_code, answer_object)


def create_app(
    *, settings: AgentSettings, answerer: NewsAgent, lifespan=None
) -> FastAPI:
    """Build the agent's FastAPI application.

    Dependencies are passed in rather than read from globals so tests can run
    the API against a stub answerer and an in-memory database.
    """
    app = FastAPI(
        title="AI RSS News Assistant agent",
        # This is an internal API: no interactive docs and no schema endpoint.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.answerer = answerer
    app.include_router(router)
    return app
