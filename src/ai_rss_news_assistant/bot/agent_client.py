"""HTTP client the bot uses to reach the agent service.

The bot owns Telegram and nothing else: it does not retrieve news for questions,
does not build prompts, and never touches the LLM. It sends one authenticated
request to the agent's internal API and renders whatever the answer contract
says.

Failures are returned as answers, never raised. Each status is decided by the
HTTP status code -- not by parsing a response body -- and the text comes from
:mod:`ai_rss_news_assistant.contracts`, so a broken, hostile, or truncated
response can never put its own content into a Telegram message. The bearer token
travels only in the ``Authorization`` header and is never logged.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from ai_rss_news_assistant.contracts import AgentAnswer, AnswerRequest, AnswerStatus

logger = logging.getLogger(__name__)

ANSWER_PATH = "/answer"
HEALTH_PATH = "/health"

# HTTP status -> answer status. 401/403 means the bot itself is misconfigured
# (wrong or missing token), which the user experiences as "not available";
# 422 means our own validation disagreed with the agent's, which is a bug on
# this side rather than something the user can influence.
_STATUS_BY_HTTP_CODE: dict[int, AnswerStatus] = {
    401: "unavailable",
    403: "unavailable",
    422: "provider_error",
    502: "provider_error",
    503: "unavailable",
    504: "timeout",
}


class UnavailableAnswerer:
    """Answerer used when no agent is configured for this process.

    It never contacts anything and reports the same status the API would report
    if it had no provider, so /ask degrades with a truthful message instead of
    failing.
    """

    async def answer(self, user_id: uuid.UUID, question: str) -> AgentAnswer:
        """Report that answering is unavailable without doing any work."""
        return AgentAnswer.for_status("unavailable")


class AgentHTTPClient:
    """Answers questions by calling the agent's internal HTTP API."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 40,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def answer(self, user_id: uuid.UUID, question: str) -> AgentAnswer:
        """Ask the agent one question and map the outcome to an answer."""
        request = AnswerRequest(user_id=user_id, question=question)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}{ANSWER_PATH}",
                    json=request.to_payload(),
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.TimeoutException:
            logger.warning("Agent request timed out after %ss", self._timeout_seconds)
            return AgentAnswer.for_status("timeout")
        except httpx.HTTPError:
            logger.warning("Agent is unreachable")
            return AgentAnswer.for_status("unavailable")

        if response.status_code == httpx.codes.OK:
            return _parse_answer(response)

        status_name = _STATUS_BY_HTTP_CODE.get(response.status_code)
        if status_name is None:
            logger.warning("Agent returned unexpected HTTP %s", response.status_code)
            return AgentAnswer.for_status("provider_error")

        logger.warning("Agent returned HTTP %s", response.status_code)
        return AgentAnswer.for_status(status_name)


def _parse_answer(response: httpx.Response) -> AgentAnswer:
    """Parse a 200 response, treating anything malformed as a provider failure."""
    try:
        payload = response.json()
    except ValueError:
        logger.warning("Agent returned a body that is not JSON")
        return AgentAnswer.for_status("provider_error")

    try:
        return AgentAnswer.from_payload(payload)
    except (TypeError, ValueError):
        logger.warning("Agent returned a payload that does not match the contract")
        return AgentAnswer.for_status("provider_error")
