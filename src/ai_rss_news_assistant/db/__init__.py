"""Database package for the AI RSS News Assistant."""

from __future__ import annotations

from .base import Base, Database, init_database, get_database

__all__ = ["Base", "Database", "init_database", "get_database"]
