"""Worker-side behavior: RSS ingestion support, summarization, and delivery.

These modules run in the worker process, which polls feeds, summarizes new
articles, and sends them to users with automatic delivery enabled. Nothing here
may import the Telegram bot entry point, so the worker stays runnable without a
bot token.
"""

from __future__ import annotations
