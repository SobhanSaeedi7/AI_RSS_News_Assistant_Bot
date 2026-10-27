"""Minimal OpenAI-compatible chat completions client.

The same client is used by the worker (news summarization) and by the bot
(grounded question answering). On HTTP failure only the status code and a
bounded, sanitized subset of the provider's structured error (``type``,
``code``, ``message``) are surfaced; request headers, request bodies, and the
full response body are never logged or returned to callers.

Every failure is raised as :class:`LLMError` with a ``kind`` of ``"timeout"``
or ``"provider"``, so callers can react to a slow provider without parsing the
message text.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Literal, Protocol

from ai_rss_news_assistant.config import Settings

MAX_RESPONSE_BYTES = 256 * 1024
MAX_ERROR_BODY_BYTES = 8 * 1024
MAX_ERROR_MESSAGE_CHARS = 200
MAX_ERROR_FIELD_CHARS = 64
DEFAULT_TIMEOUT_SECONDS = 30

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]+")
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")
_AUTHORIZATION_RE = re.compile(r"(?i)\bauthorization\b\s*[:=]\s*[^,;]+")
_CREDENTIAL_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|token)\b\s*[:=]\s*[^\s,;]+"
)


LLMErrorKind = Literal["timeout", "provider"]


class LLMError(Exception):
    """Raised when a chat completion cannot be produced.

    ``kind`` classifies the failure so callers do not have to parse the
    message: ``"timeout"`` means the provider did not answer within the
    configured timeout, and ``"provider"`` covers every other cause (HTTP
    errors, connection failures, unusable responses). Nothing but a bounded
    reason is stored, so no key or header can leak through it.
    """

    def __init__(self, message: str, *, kind: LLMErrorKind = "provider") -> None:
        super().__init__(message)
        self.kind: LLMErrorKind = kind


def _sanitize_error_text(value: object, api_key: str, limit: int) -> str:
    """Collapse whitespace, redact credentials, and bound the text length."""
    text = " ".join(str(value).split())
    if api_key:
        text = text.replace(api_key, "[redacted]")
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _SK_KEY_RE.sub("[redacted]", text)
    text = _AUTHORIZATION_RE.sub("[redacted]", text)
    text = _CREDENTIAL_RE.sub("[redacted]", text)
    if len(text) > limit:
        text = text[: max(limit - 3, 0)] + "..."
    return text


def _provider_error_fields(body: bytes) -> dict[str, str]:
    """Return the provider's structured ``type``/``code``/``message`` fields."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}

    error = payload.get("error")
    source = error if isinstance(error, dict) else payload
    fields: dict[str, str] = {}
    for name in ("type", "code", "message"):
        value = source.get(name)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        fields[name] = str(value)
    return fields


def _timed_out(exc: BaseException) -> bool:
    """Return True when a urllib failure was caused by the request timeout.

    ``urlopen`` raises :class:`TimeoutError` (``socket.timeout``) directly when
    the connection or a read times out, and wraps a timeout during the request
    in ``URLError(reason=TimeoutError(...))``, so both shapes are checked.
    """
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(getattr(exc, "reason", None), TimeoutError)


def _http_error_message(exc: urllib.error.HTTPError, api_key: str) -> str:
    """Build a bounded, redacted error message from an HTTP error response."""
    base = f"LLM request failed with HTTP {exc.code}"
    try:
        body = exc.read(MAX_ERROR_BODY_BYTES)
    except Exception:  # noqa: BLE001 - a broken error body must not hide the status
        return base
    if not isinstance(body, bytes):
        return base

    fields = _provider_error_fields(body)
    details: list[str] = []
    for name, limit in (
        ("type", MAX_ERROR_FIELD_CHARS),
        ("code", MAX_ERROR_FIELD_CHARS),
        ("message", MAX_ERROR_MESSAGE_CHARS),
    ):
        if name not in fields:
            continue
        value = _sanitize_error_text(fields[name], api_key, limit)
        if value:
            details.append(f"{name}={value}")
    if not details:
        return base
    return f"{base} ({', '.join(details)})"


class ChatClient(Protocol):
    """Sends a chat conversation to a provider and returns the reply text."""

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int = 400,
        temperature: float = 0.2,
    ) -> str:
        """Return the assistant reply, or raise :class:`LLMError`."""
        ...


class OpenAICompatibleClient:
    """Client for an OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int = 400,
        temperature: float = 0.2,
    ) -> str:
        body = json.dumps(
            {
                "model": self._model,
                "messages": list(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        ).encode("utf-8")
        return await asyncio.to_thread(self._request, body)

    def _request(self, body: bytes) -> str:
        """Perform the blocking HTTP request. Never logs the API key."""
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            # Surface only a bounded, sanitized subset of the provider's
            # structured error. Headers, request bodies, and the full response
            # body are never logged or returned.
            raise LLMError(_http_error_message(exc, self._api_key)) from None
        except Exception as exc:  # noqa: BLE001 - normalise urllib failures
            if _timed_out(exc):
                raise LLMError(
                    f"LLM request timed out after {self._timeout_seconds}s",
                    kind="timeout",
                ) from None
            raise LLMError("LLM request failed") from None

        try:
            payload = json.loads(raw.decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("LLM response was not understood") from None

        if not isinstance(content, str):
            raise LLMError("LLM response was not understood")
        return " ".join(content.split())


def build_chat_client(settings: Settings) -> OpenAICompatibleClient | None:
    """Return a client when an API key is configured, otherwise ``None``."""
    if not settings.llm_api_key:
        return None
    return OpenAICompatibleClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
