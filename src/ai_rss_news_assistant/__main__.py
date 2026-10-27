"""Allow running the package as ``python -m ai_rss_news_assistant <service>``.

Services are ``bot`` (the default, also reachable as ``main`` for backwards
compatibility), ``worker``, and ``agent``. Each one is imported lazily so that
starting a service never imports -- or validates the configuration of -- another
service.
"""

from __future__ import annotations

import sys


def _run(command: str) -> None:
    """Run the requested service; unknown commands keep the historical default."""
    if command == "worker":
        from ai_rss_news_assistant.worker.entrypoint import run_worker

        run_worker()
        return

    if command == "agent":
        from ai_rss_news_assistant.agent.entrypoint import run_agent

        run_agent()
        return

    from ai_rss_news_assistant.bot.entrypoint import run_bot

    run_bot()


if __name__ == "__main__":
    _run(sys.argv[1] if len(sys.argv) > 1 else "main")
