"""Database package: models, session primitives, and data access.

The package exposes the declarative :class:`Base` and the :class:`Database`
container. Services construct their own :class:`Database` and pass it (or its
session factory) down explicitly; there is no process-wide instance here.
"""

from __future__ import annotations

from .base import Base, Database

__all__ = ["Base", "Database"]
