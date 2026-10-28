"""Supported automatic-delivery intervals and their user-facing labels.

The set is deliberately small and fixed. A user picks a cadence, not a
free-form number of seconds, so the command can validate input completely and
every choice is a value the worker can honour. ``0`` means "no throttle": the
user receives new items on the next poll, which is how delivery worked before
per-user scheduling existed.

This module is pure (no I/O, no database, no Telegram) so the parser and the
labels can be unit tested directly.

Timing granularity: the worker runs every ``POLL_INTERVAL_SECONDS`` (default
300 s), so an interval is a *minimum* spacing. A "15m" schedule set at 12:07 is
first eligible at 12:22 and is actually delivered at the first poll at or after
that moment - 12:25 with the default settings.
"""

from __future__ import annotations

DEFAULT_DELIVERY_INTERVAL_SECONDS = 0

# Every value a user may choose, in seconds.
DELIVERY_INTERVALS: tuple[int, ...] = (
    0,  # immediately, once per poll (default)
    900,  # 15m
    1800,  # 30m
    3600,  # 1h
    10800,  # 3h
    21600,  # 6h
    43200,  # 12h
    86400,  # 24h
)

# Canonical command tokens, in the order they are shown to the user.
INTERVAL_TOKENS: tuple[str, ...] = (
    "0",
    "15m",
    "30m",
    "1h",
    "3h",
    "6h",
    "12h",
    "24h",
)

_TOKEN_TO_SECONDS: dict[str, int] = dict(
    zip(INTERVAL_TOKENS, DELIVERY_INTERVALS, strict=True)
)

# Friendly aliases. "فوری" is offered because the bot speaks Persian, and
# "immediate" is the English spelling documented in the README. There is no
# "off" alias: delivery is switched off with /delivery, and overloading the
# interval parser with the same word made the two settings ambiguous.
_ALIASES: dict[str, int] = {
    **_TOKEN_TO_SECONDS,
    "immediate": 0,
    "فوری": 0,
}

_LABELS: dict[int, str] = {
    0: "فوری (در هر poll)",
    900: "هر 15 دقیقه",
    1800: "هر 30 دقیقه",
    3600: "هر 1 ساعت",
    10800: "هر 3 ساعت",
    21600: "هر 6 ساعت",
    43200: "هر 12 ساعت",
    86400: "هر 24 ساعت",
}


def parse_interval(raw: str) -> int | None:
    """Return the interval in seconds, or ``None`` if it is not allowed."""
    return _ALIASES.get(raw.strip().lower())


def format_interval(seconds: int) -> str:
    """Return the Persian label for an interval.

    An unknown stored value (possible only if the database was edited by hand)
    is rendered as the number of seconds rather than crashing a handler.
    """
    return _LABELS.get(seconds, f"هر {seconds} ثانیه")


def interval_options() -> str:
    """Return the allowed tokens as one comma-separated line."""
    return ", ".join(INTERVAL_TOKENS)
