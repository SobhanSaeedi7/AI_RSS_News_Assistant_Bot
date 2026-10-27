"""Run the worker process directly.

Supports ``python -m ai_rss_news_assistant.worker`` so the worker service can
start without importing the bot entry point.
"""

from __future__ import annotations

from ai_rss_news_assistant.worker.entrypoint import run_worker

if __name__ == "__main__":
    run_worker()
