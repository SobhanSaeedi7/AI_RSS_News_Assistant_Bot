"""Database package for the AI RSS News Assistant."""

from __future__ import annotations

from .base import Base, Database, get_database, init_database

__all__ = ["Base", "Database", "get_database", "init_database"]
