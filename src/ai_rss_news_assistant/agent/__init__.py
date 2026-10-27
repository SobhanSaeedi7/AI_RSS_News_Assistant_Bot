"""Agent service: grounded answers over one user's stored news.

This package owns retrieval, the LLM prompt, and the internal HTTP API. It must
not own Telegram handling, RSS ingestion, or delivery, and it must not import
the bot or the worker packages: the bot reaches it over HTTP, not by import.

Submodules are imported explicitly (``agent.answer``, ``agent.api``) rather
than re-exported here, so importing the package never pulls in the LLM client
or the database.
"""

from __future__ import annotations
