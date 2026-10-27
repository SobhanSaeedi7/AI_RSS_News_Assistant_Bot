"""Database engine, session factory, and declarative base.

The :class:`Database` class owns a SQLAlchemy async engine and session factory.
Each service entry point (``bot``, ``worker``, ``agent``) constructs one from the
``DATABASE_URL`` setting and passes it -- or its session factory -- explicitly to
the objects that need it. There is deliberately no module-level database: a
shared global would let a process-wide instance leak between services and make
tests depend on import order. Tests construct independent :class:`Database`
instances against in-memory or file-backed databases.
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
