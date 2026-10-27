"""Agent error types and the mapping to safe HTTP responses.

The HTTP layer must never return a stack trace, an exception repr, or a
provider response body to its caller. Failures the agent owns are therefore
expressed as one of the types below, each carrying the HTTP status and the
public error code the API answers with, and
:func:`safe_error_payload` renders the only thing that may leave the process: a
short code plus a generic message that is already shown to Telegram users.

The mapping between answer statuses and HTTP status codes lives here too, so
the agent's API and the bot's client cannot disagree about what a code means.
"""

from __future__ import annotations

from ai_rss_news_assistant.contracts import (
    AGENT_ERROR,
    AGENT_UNAVAILABLE,
    AnswerStatus,
)
from ai_rss_news_assistant.llm import LLMErrorKind


class AgentError(Exception):
    """Base class for a failure that must become a safe HTTP response."""

    http_status: int = 500
    code: str = "internal_error"
    public_message: str = AGENT_ERROR

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.public_message)


class AgentAuthError(AgentError):
    """The caller did not present a valid internal bearer token."""

    http_status = 401
    code = "unauthorized"
    public_message = "دسترسی به سرویس پاسخ مجاز نیست."


class AgentConfigurationError(AgentError):
    """A setting the agent needs to answer questions is missing."""

    http_status = 503
    code = "agent_unavailable"
    public_message = AGENT_UNAVAILABLE


class AgentUpstreamError(AgentError):
    """The LLM provider failed. ``kind`` comes from :class:`LLMError`."""

    http_status = 502
    code = "upstream_error"
    public_message = AGENT_ERROR

    def __init__(self, message: str = "", *, kind: LLMErrorKind = "provider") -> None:
        super().__init__(message)
        self.kind: LLMErrorKind = kind


class AgentTimeoutError(AgentUpstreamError):
    """The LLM provider did not answer within the configured timeout."""

    http_status = 504
    code = "upstream_timeout"
    public_message = AGENT_ERROR

    def __init__(self, message: str = "") -> None:
        super().__init__(message, kind="timeout")


def safe_error_payload(error: AgentError) -> dict[str, str]:
    """Return the only error body the API may send.

    The exception's own message is deliberately dropped: callers get the public
    code and the generic user-facing text, never diagnostics or provider output.
    """
    return {"error": error.code, "message": error.public_message}


# Answer status to HTTP status. Both grounded answers and an explicit
# insufficient-evidence answer are successful responses; the remaining codes
# tell the bot how to react without parsing any text.
ANSWER_STATUS_HTTP_STATUS: dict[AnswerStatus, int] = {
    "answered": 200,
    "insufficient_evidence": 200,
    "empty_question": 200,
    "unavailable": 503,
    "timeout": 504,
    "provider_error": 502,
}

# A 200 response carries its own answer status in the body, because both a
# grounded answer and an explicit insufficient-evidence answer succeed; so only
# the failure codes are mapped here.
_HTTP_STATUS_ANSWER_STATUS: dict[int, AnswerStatus] = {
    401: "unavailable",
    502: "provider_error",
    503: "unavailable",
    504: "timeout",
}


def answer_status_for_http_status(status_code: int) -> AnswerStatus:
    """Return the answer status a failed response code represents.

    A successful response (200) is deliberately not mapped: its body states the
    status. Any code the agent does not document is treated as an upstream
    failure rather than as an answer.
    """
    return _HTTP_STATUS_ANSWER_STATUS.get(status_code, "provider_error")


def answer_status_for_llm_error(kind: LLMErrorKind) -> AnswerStatus:
    """Translate an LLM failure classification into an answer status.

    This is the only place a provider failure becomes an answer status, so no
    caller has to inspect exception text to tell a timeout from a rejection.
    """
    return "timeout" if kind == "timeout" else "provider_error"
