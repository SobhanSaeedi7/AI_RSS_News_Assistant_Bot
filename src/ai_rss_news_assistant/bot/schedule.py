"""Numeric automatic-delivery intervals for ``/schedule``.

A user sets one non-negative integer number of minutes. ``0`` means "no
throttle": delivery is eligible on every worker poll, which is how delivery
worked before per-user scheduling existed. A positive value is the *minimum*
spacing between two automatic deliveries for that user.

Timing granularity: the worker runs every ``POLL_INTERVAL_SECONDS`` (default
300 s), so an interval is a minimum, not a timer. A 30-minute schedule set at
12:07 is first eligible at 12:37 and is actually delivered at the first poll at
or after that moment - 12:40 with the default settings. An interval below the
poll interval cannot guarantee delivery that often.

The upper bound is the PostgreSQL ``INTEGER`` (signed 32-bit) limit of the
``users.delivery_interval_seconds`` column. ``MAX_INTERVAL_MINUTES * 60`` fits
in that column, so every accepted value can be persisted without overflow.

This module is pure (no I/O, no database, no Telegram) so the parser and the
labels can be unit tested directly.
"""

from __future__ import annotations

DEFAULT_DELIVERY_INTERVAL_SECONDS = 0
SECONDS_PER_MINUTE = 60

# ``delivery_interval_seconds`` is a SQLAlchemy Integer (PostgreSQL int4).
# Anything larger would overflow when stored, so the accepted minutes range is
# bounded by the largest whole number of minutes that fits in int4.
MAX_INTERVAL_MINUTES = (2**31 - 1) // SECONDS_PER_MINUTE


def parse_interval(raw: str) -> int | None:
    """Return the interval in seconds, or ``None`` if the input is invalid.

    Exactly one non-negative base-10 integer is accepted. Suffixes (``m``,
    ``h``, ``d``), signs, decimals, and embedded whitespace are rejected rather
    than truncated or rounded. Persian/Arabic-Indic digits are accepted because
    ``int`` understands them and the bot speaks Persian.
    """
    value = raw.strip()
    if not value or not value.isdigit():
        return None

    try:
        minutes = int(value)
    except ValueError:
        # ``str.isdigit`` accepts a few characters (for example superscripts)
        # that ``int`` cannot parse; treat those as invalid input.
        return None

    if minutes > MAX_INTERVAL_MINUTES:
        return None
    return minutes * SECONDS_PER_MINUTE


def format_interval(seconds: int) -> str:
    """Return a Persian label for an interval stored in seconds.

    Values produced by :func:`parse_interval` are always whole minutes. An
    unknown stored value (possible only if the database was edited by hand) is
    rendered as seconds rather than crashing a handler.
    """
    if seconds == 0:
        return "0 دقیقه (بدون محدودیت؛ در هر poll)"
    if seconds % SECONDS_PER_MINUTE == 0:
        return f"هر {seconds // SECONDS_PER_MINUTE:,} دقیقه"
    return f"هر {seconds} ثانیه"
