"""Cross-service contracts for grounded answers.

The bot and the agent run as separate processes and talk over internal HTTP, so
the shape of a question and of an answer has to live somewhere both can import
without either depending on the other's implementation. This is that module.

It contains the closed set of answer statuses, the canonical user-facing texts
for answers that are not grounded, the answer and source dataclasses, and the
JSON payload helpers both sides of the wire use. Keeping the texts here means
the agent's in-process answers and the bot's fallbacks for a failed agent call
cannot drift apart.

Nothing here imports the database, the LLM client, or any service package, and
nothing here performs I/O. Payload parsing raises ``TypeError`` for a value of
the wrong type and ``ValueError`` for a value of the right type with a bad
content, so a client can tell "not our protocol" from "malformed payload".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

# The question limit is part of the contract: the bot refuses longer questions
# and the agent's HTTP layer rejects them with 422.
MAX_QUESTION_CHARS = 500

AnswerStatus = Literal[
    "answered",
    "insufficient_evidence",
    "empty_question",
    "unavailable",
    "timeout",
    "provider_error",
]

INSUFFICIENT_EVIDENCE = (
    "در خبرهای ذخیره‌شده شما اطلاعات کافی برای پاسخ به این سؤال پیدا نشد."
)
AGENT_UNAVAILABLE = "قابلیت پاسخ هوشمند در حال حاضر فعال نیست."
AGENT_ERROR = "در حال حاضر امکان پاسخ‌دهی نیست. لطفاً کمی بعد دوباره تلاش کنید."
EMPTY_QUESTION = "لطفاً سؤال خود را بنویسید."

# Text shown for every status that is not a grounded answer. The agent uses it
# to answer, and the bot uses it when it cannot reach the agent at all.
ANSWER_TEXT_BY_STATUS: dict[str, str] = {
    "insufficient_evidence": INSUFFICIENT_EVIDENCE,
    "empty_question": EMPTY_QUESTION,
    "unavailable": AGENT_UNAVAILABLE,
    "timeout": AGENT_ERROR,
    "provider_error": AGENT_ERROR,
}

_KNOWN_STATUSES = frozenset(ANSWER_TEXT_BY_STATUS) | {"answered"}

# --- HTTP transport mapping -------------------------------------------------
#
# One authoritative pair of tables, imported by both sides of the wire:
#
# * the agent turns an answer status into an HTTP status (``/answer``);
# * the bot turns a failed HTTP status back into an answer status.
#
# They live here, next to the statuses themselves, so the two services cannot
# disagree about what a code means. Keeping a copy in each service is exactly
# the drift this module exists to prevent.

ANSWER_STATUS_HTTP_STATUS: dict[AnswerStatus, int] = {
    "answered": 200,
    "insufficient_evidence": 200,
    "empty_question": 200,
    "unavailable": 503,
    "timeout": 504,
    "provider_error": 502,
}

# Only failures are mapped back: a 200 response carries its status in the body,
# because both a grounded answer and an insufficient-evidence answer succeed.
_FAILURE_HTTP_ANSWER_STATUS: dict[int, AnswerStatus] = {
    # Missing/wrong internal token or an unconfigured provider: the caller
    # cannot ask right now, which is different from "the answer failed".
    401: "unavailable",
    403: "unavailable",
    422: "provider_error",  # our own validation disagreed: a bug on this side
    500: "provider_error",
    502: "provider_error",
    503: "unavailable",
    504: "timeout",
}

FAILURE_HTTP_STATUSES = frozenset(_FAILURE_HTTP_ANSWER_STATUS)


def answer_status_for_http_status(status_code: int) -> AnswerStatus:
    """Return the answer status a failed HTTP response represents.

    A successful response is deliberately not mapped: its body states the
    status. Any code the agent does not document is treated as an upstream
    failure rather than as an answer.
    """
    return _FAILURE_HTTP_ANSWER_STATUS.get(status_code, "provider_error")


def answer_status_for_llm_error(kind: str) -> AnswerStatus:
    """Translate an LLM failure classification into an answer status.

    ``kind`` is :attr:`ai_rss_news_assistant.llm.LLMError.kind`, passed as a
    plain string so this module keeps its no-dependency contract and the bot
    can import it without importing the provider client.
    """
    return "timeout" if kind == "timeout" else "provider_error"


@dataclass(frozen=True)
class AgentSource:
    """A stored news item cited by an agent answer.

    ``citation_index`` is the number the model used in the answer (``[2]`` ->
    ``2``). It is optional so a payload from an older agent, or a source built
    by hand, still parses; the bot then falls back to the source's position.
    """

    title: str
    url: str | None
    feed_title: str | None = None
    published_at: datetime | None = None
    citation_index: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-serialisable form sent over the wire."""
        return {
            "title": self.title,
            "url": self.url,
            "feed_title": self.feed_title,
            "published_at": (
                self.published_at.isoformat() if self.published_at else None
            ),
            "citation_index": self.citation_index,
        }

    @classmethod
    def from_payload(cls, data: object) -> AgentSource:
        """Rebuild a source from a payload.

        Raises ``TypeError`` when a value has the wrong type and ``ValueError``
        when a timestamp cannot be parsed.
        """
        if not isinstance(data, dict):
            raise TypeError("source must be an object")
        title = data.get("title")
        url = data.get("url")
        feed_title = data.get("feed_title")
        published_at = data.get("published_at")
        citation_index = data.get("citation_index")
        if not isinstance(title, str):
            raise TypeError("source title must be a string")
        if url is not None and not isinstance(url, str):
            raise TypeError("source url must be a string or null")
        if feed_title is not None and not isinstance(feed_title, str):
            raise TypeError("source feed_title must be a string or null")
        if citation_index is not None and (
            not isinstance(citation_index, int)
            or isinstance(citation_index, bool)
            or citation_index < 1
        ):
            raise TypeError("source citation_index must be a positive integer or null")

        parsed_published_at: datetime | None = None
        if isinstance(published_at, str):
            try:
                parsed_published_at = datetime.fromisoformat(published_at)
            except ValueError:
                raise ValueError("source published_at is not a timestamp") from None
        elif published_at is not None:
            raise TypeError("source published_at must be a string or null")

        return cls(
            title=title,
            url=url,
            feed_title=feed_title,
            published_at=parsed_published_at,
            citation_index=citation_index,
        )


@dataclass(frozen=True)
class AgentAnswer:
    """One answer, ready to be rendered or transported.

    ``status`` is the machine-readable outcome: only ``"answered"`` carries
    usable ``text`` from the provider, and only then may ``sources`` be shown.
    """

    text: str
    sources: list[AgentSource] = field(default_factory=list)
    grounded: bool = False
    status: AnswerStatus = "answered"

    @classmethod
    def for_status(
        cls, status: AnswerStatus, *, text: str | None = None
    ) -> AgentAnswer:
        """Build the canonical answer for a non-grounded status."""
        return cls(
            text=text or ANSWER_TEXT_BY_STATUS.get(status, AGENT_ERROR),
            status=status,
        )

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-serialisable form sent over the wire."""
        return {
            "status": self.status,
            "text": self.text,
            "grounded": self.grounded,
            "sources": [source.to_payload() for source in self.sources],
        }

    @classmethod
    def from_payload(cls, data: object) -> AgentAnswer:
        """Rebuild an answer from a payload.

        Raises ``TypeError`` when a value has the wrong type and ``ValueError``
        when the status is unknown or a source payload is malformed.
        """
        if not isinstance(data, dict):
            raise TypeError("answer must be an object")
        status = data.get("status")
        text = data.get("text")
        grounded = data.get("grounded")
        sources = data.get("sources", [])
        if not isinstance(status, str):
            raise TypeError("answer status must be a string")
        if status not in _KNOWN_STATUSES:
            raise ValueError("answer status is not recognised")
        if not isinstance(text, str):
            raise TypeError("answer text must be a string")
        if not isinstance(grounded, bool):
            raise TypeError("answer grounded must be a boolean")
        if not isinstance(sources, list):
            raise TypeError("answer sources must be a list")
        return cls(
            text=text,
            sources=[AgentSource.from_payload(source) for source in sources],
            grounded=grounded,
            status=cast(AnswerStatus, status),
        )


@dataclass(frozen=True)
class AnswerRequest:
    """One question asked of the agent for one user."""

    user_id: uuid.UUID
    question: str

    def to_payload(self) -> dict[str, str]:
        """Return the JSON-serialisable form sent over the wire."""
        return {"user_id": str(self.user_id), "question": self.question}

    @classmethod
    def from_payload(cls, data: object) -> AnswerRequest:
        """Rebuild a request from a payload.

        Raises ``TypeError`` when a value has the wrong type and ``ValueError``
        when the user id is not a UUID.
        """
        if not isinstance(data, dict):
            raise TypeError("request must be an object")
        user_id = data.get("user_id")
        question = data.get("question")
        if not isinstance(user_id, str):
            raise TypeError("user_id must be a string")
        if not isinstance(question, str):
            raise TypeError("question must be a string")
        try:
            parsed_user_id = uuid.UUID(user_id)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("user_id must be a UUID") from None
        return cls(user_id=parsed_user_id, question=question)
