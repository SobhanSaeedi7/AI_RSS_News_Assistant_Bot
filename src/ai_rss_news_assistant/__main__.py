"""Allow running the package as ``python -m ai_rss_news_assistant [main|worker]``."""

from __future__ import annotations

import sys

from .main import main, worker

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "main"
    if command == "worker":
        worker()
    else:
        main()
