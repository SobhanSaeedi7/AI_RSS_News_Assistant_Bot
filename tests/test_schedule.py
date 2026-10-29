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
    MAX_INTERVAL_MINUTES,
    SECONDS_PER_MINUTE,
    format_interval,
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


# --- numeric interval parser ------------------------------------------------


def test_default_interval_preserves_the_previous_behaviour() -> None:
    """Before scheduling existed every poll could deliver, so the default is 0."""
    assert DEFAULT_DELIVERY_INTERVAL_SECONDS == 0


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("0", 0),
        ("30", 1800),
        ("60", 3600),
        ("030", 1800),
        (" 45 ", 2700),
        ("۲۵", 1500),  # Persian digits are accepted by ``int``.
    ],
)
def test_integer_minutes_parse_to_seconds(raw: str, seconds: int) -> None:
    assert parse_interval(raw) == seconds


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "5m", "1h", "1d", "1.5", "-1", "+5", "abc", "30 60", "weekly"],
)
def test_invalid_values_are_rejected(raw: str) -> None:
    """Suffixes, signs, decimals, and non-numeric input are never truncated."""
    assert parse_interval(raw) is None


def test_maximum_interval_fits_the_database_column() -> None:
    """The upper bound is the PostgreSQL int4 limit of the seconds column."""
    assert MAX_INTERVAL_MINUTES * SECONDS_PER_MINUTE <= 2**31 - 1
    assert (MAX_INTERVAL_MINUTES + 1) * SECONDS_PER_MINUTE > 2**31 - 1

    assert parse_interval(str(MAX_INTERVAL_MINUTES)) == (
        MAX_INTERVAL_MINUTES * SECONDS_PER_MINUTE
    )
    assert parse_interval(str(MAX_INTERVAL_MINUTES + 1)) is None


def test_minutes_are_rendered_as_minutes() -> None:
    assert format_interval(0) != format_interval(1800)
    assert "0" in format_interval(0)
    assert "بدون محدودیت" in format_interval(0)
    assert format_interval(1800) == "هر 30 دقیقه"
    assert format_interval(3600) == "هر 60 دقیقه"


def test_unknown_stored_interval_does_not_crash_rendering() -> None:
    """A hand-edited row must not break /schedule."""
    assert format_interval(42) == "هر 42 ثانیه"


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


def test_the_help_text_documents_the_numeric_syntax() -> None:
    """/help must be self-sufficient: the numeric usage and example are visible."""
    from ai_rss_news_assistant.bot.messages import HELP_MESSAGE

    assert "/schedule" in HELP_MESSAGE
    assert "/schedule 30" in HELP_MESSAGE
    assert "دقیقه" in HELP_MESSAGE
    assert "poll" in HELP_MESSAGE
