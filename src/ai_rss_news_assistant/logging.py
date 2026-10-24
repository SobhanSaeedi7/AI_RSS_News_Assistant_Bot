"""Application logging configuration.

Logging is configured once at process startup and writes structured, human
readable lines to stdout so container log drivers can capture them.
"""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger.

    Safe to call multiple times; ``force=True`` reconfigures the root logger
    instead of being silently ignored on subsequent calls.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )

    # httpx logs every outbound request URL at INFO, and Telegram API URLs
    # embed the bot token (https://api.telegram.org/bot<token>/<method>).
    # Keep this third-party logger at WARNING so credentials never reach the
    # logs; application loggers, warnings, errors, and tracebacks are
    # unaffected.
    logging.getLogger("httpx").setLevel(logging.WARNING)
