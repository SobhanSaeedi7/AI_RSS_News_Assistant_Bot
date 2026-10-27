"""AI RSS News Assistant: Telegram bot, RSS worker, and grounded Q&A agent.

Nothing is imported here on purpose. Each service lives in its own subpackage
(``bot``, ``worker``, ``agent``) and is imported by the entry point that runs it,
so importing this package -- or one service's modules -- never pulls in another
service or validates configuration it does not need.

This also fixes a name collision: the root used to re-export a ``worker``
function from ``main.py``, which shadowed the ``worker`` subpackage so that
``import ai_rss_news_assistant.worker`` returned a function. Service entry points
are now reached through their modules (or the console scripts declared in
``pyproject.toml``); ``ai_rss_news_assistant.main`` remains as a compatibility
facade for the historical ``main``/``worker`` names.
"""

from __future__ import annotations
