"""Allow running the package as ``python -m ai_rss_news_assistant [main|worker]``.

Each command is imported lazily so that starting one service never imports (or
validates the configuration of) another service.
"""

from __future__ import annotations

import sys


def _run(command: str) -> None:
    """Run the requested service; unknown commands keep the historical default."""
    if command == "worker":
        from ai_rss_news_assistant.worker.entrypoint import run_worker

        run_worker()
        return

    from ai_rss_news_assistant.main import main

    main()


if __name__ == "__main__":
    _run(sys.argv[1] if len(sys.argv) > 1 else "main")
