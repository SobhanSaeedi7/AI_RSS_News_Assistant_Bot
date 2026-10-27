"""Bot service: Telegram transport, user-facing texts, and use cases.

This package owns everything a Telegram user can trigger -- commands, feed
management, the delivery preference, and the question-answering flow -- but not
retrieval, summarization, or the LLM: those belong to the worker and the agent,
and the bot reaches the agent over internal HTTP rather than by import.

Submodules are imported explicitly (``bot.handlers``, ``bot.use_cases``,
``bot.entrypoint``) so importing the package never builds a Telegram
application or validates a service's configuration.
"""

from __future__ import annotations
