"""Per-user automatic-delivery scheduling.

Covers three layers without Telegram or network access:

* the pure interval catalog and parser (``bot.schedule``);
* the due-time rule the worker applies (``worker.delivery.schedule_is_due``);
* per-user persistence through the bot use cases, against a real (in-memory)
  database, including the default for a brand-new user.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from ai_rss_news_assistant.bot import use_cases
from ai_rss_news_assistant.bot.schedule import (
    DEFAULT_DELIVERY_INTERVAL_SECONDS,
    DELIVERY_INTERVALS,
    format_interval,
    interval_options,
    parse_interval,
)
from ai_rss_news_assistant.db.base import Base
from ai_rss_news_assistant.worker.delivery import schedule_is_due


class _TestDatabase:
    """Minimal stand-in for :class:`Database` over an in-memory engine.

    It mirrors ``Database.session`` (commit on success, rollback on error)
    without creating a second connection to a fresh in-memory database.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    @contextlib.asynccontextmanager
    async def session(self):
        async with self._factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


async def _make_database() -> tuple[object, _TestDatabase]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, _TestDatabase(factory)


# --- interval catalog -------------------------------------------------------


def test_default_interval_preserves_the_previous_behaviour() -> None:
    """Before scheduling existed every poll could deliver, so the default is 0."""
    assert DEFAULT_DELIVERY_INTERVAL_SECONDS == 0
    assert DELIVERY_INTERVALS[0] == 0


def test_all_supported_tokens_parse_to_their_seconds() -> None:
    expected = {
        "0": 0,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "3h": 10800,
        "6h": 21600,
        "12h": 43200,
        "24h": 86400,
    }
    for token, seconds in expected.items():
        assert parse_interval(token) == seconds


def test_parsing_is_case_and_whitespace_insensitive() -> None:
    assert parse_interval(" 6H ") == 21600
    assert parse_interval("فوری") == 0


@pytest.mark.parametrize("raw", ["5m", "90m", "1d", "", "  ", "3600", "weekly"])
def test_unsupported_values_are_rejected(raw: str) -> None:
    """Only the documented presets are accepted, not arbitrary durations."""
    assert parse_interval(raw) is None


def test_every_supported_interval_has_a_label() -> None:
    for seconds in DELIVERY_INTERVALS:
        assert format_interval(seconds)
    assert format_interval(0) != format_interval(3600)


def test_unknown_stored_interval_does_not_crash_rendering() -> None:
    """A hand-edited row must not break /schedule."""
    assert format_interval(42) == "هر 42 ثانیه"


def test_options_line_lists_every_token() -> None:
    options = interval_options()
    for token in ("0", "15m", "1h", "6h", "24h"):
        assert token in options


# --- due-time rule ----------------------------------------------------------


def test_zero_interval_is_always_due() -> None:
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert schedule_is_due(0, now - timedelta(seconds=1), now) is True
    assert schedule_is_due(0, None, now) is True


def test_never_delivered_is_due_immediately() -> None:
    """A long interval must not delay the very first message."""
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert schedule_is_due(86400, None, now) is True


def test_interval_is_enforced_to_the_second() -> None:
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    assert schedule_is_due(3600, now - timedelta(minutes=59, seconds=59), now) is False
    assert schedule_is_due(3600, now - timedelta(hours=1), now) is True
    assert schedule_is_due(3600, now - timedelta(hours=2), now) is True


# --- persistence and per-user independence ---------------------------------


@pytest.mark.anyio
async def test_new_user_starts_with_the_default_interval() -> None:
    engine, db = await _make_database()
    try:
        assert await use_cases.delivery_interval(db, 501) == 0
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_interval_is_stored_per_user() -> None:
    engine, db = await _make_database()
    try:
        await use_cases.set_delivery_interval(db, 502, 21600)
        await use_cases.set_delivery_interval(db, 503, 900)

        assert await use_cases.delivery_interval(db, 502) == 21600
        assert await use_cases.delivery_interval(db, 503) == 900
        # A third user is unaffected and keeps the default.
        assert await use_cases.delivery_interval(db, 504) == 0
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_interval_survives_a_restart(tmp_path) -> None:
    """A brand-new engine over the same file reads the stored interval.

    This is the closest local equivalent of restarting the bot: nothing is
    cached in the process, and the second handle reads PostgreSQL-equivalent
    persisted state.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'schedule.db'}"

    first_engine = create_async_engine(url)
    async with first_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    first = _TestDatabase(
        async_sessionmaker(first_engine, class_=AsyncSession, expire_on_commit=False)
    )
    await use_cases.set_delivery_interval(first, 505, 43200)
    await first_engine.dispose()

    second_engine = create_async_engine(url)
    second = _TestDatabase(
        async_sessionmaker(second_engine, class_=AsyncSession, expire_on_commit=False)
    )
    try:
        assert await use_cases.delivery_interval(second, 505) == 43200
    finally:
        await second_engine.dispose()


@pytest.mark.anyio
async def test_changing_the_interval_keeps_the_delivery_switch_untouched() -> None:
    engine, db = await _make_database()
    try:
        await use_cases.set_delivery_preference(db, 506, False)
        await use_cases.set_delivery_interval(db, 506, 3600)

        assert await use_cases.delivery_interval(db, 506) == 3600
        assert await use_cases.delivery_preference(db, 506) is False
    finally:
        await engine.dispose()


def test_the_help_text_lists_every_accepted_interval() -> None:
    """/help must be self-sufficient: no guessing the schedule tokens."""
    from ai_rss_news_assistant.bot.messages import HELP_MESSAGE
    from ai_rss_news_assistant.bot.schedule import INTERVAL_TOKENS

    for token in INTERVAL_TOKENS:
        assert token in HELP_MESSAGE

    assert "/schedule" in HELP_MESSAGE
