"""Database engine, session factory, and declarative base.

The :class:`Database` class owns a SQLAlchemy async engine and session factory.
Application entry points (``bot`` and ``worker``) initialize a single
:class:`Database` from the ``DATABASE_URL`` setting and share it across the
process. Tests can construct independent :class:`Database` instances against
in-memory or test databases without affecting the application instance.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all domain models."""


class Database:
    """Async SQLAlchemy engine and session factory bound to a DATABASE_URL."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @property
    def engine(self):
        """Return the underlying async engine."""
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the async session factory."""
        return self._session_factory

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a transactional async session.

        The session is committed on success and rolled back on exception.
        """
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """Dispose the engine and release all pooled connections."""
        await self._engine.dispose()


# Module-level application database instance.
_db: Database | None = None


def init_database(database_url: str) -> Database:
    """Initialize the module-level database from a DATABASE_URL.

    Safe to call once at application startup. Returns the :class:`Database`
    instance so callers can keep a reference.
    """
    global _db
    _db = Database(database_url)
    return _db


def get_database() -> Database:
    """Return the module-level database, raising if not initialized."""
    if _db is None:
        raise RuntimeError("Database is not initialized. Call init_database() first.")
    return _db


@contextlib.asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session from the module-level database (DI style).

    Intended for use as a FastAPI/PTB dependency or context manager.
    """
    db = get_database()
    async with db.session() as session:
        yield session


async def dispose_database() -> None:
    """Dispose the module-level database engine."""
    global _db
    if _db is not None:
        await _db.dispose()
        _db = None
