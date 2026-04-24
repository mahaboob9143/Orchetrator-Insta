"""
core/logger.py — Structured logging for InstaAgent.

Each agent gets its own named logger via get_logger("AgentName").
Writes to console (INFO+) and two rotating log files:
  - logs/instaagent.log  (DEBUG+, 5 MB × 3 backups)
  - logs/errors.log      (ERROR+, 5 MB × 3 backups)
"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"

try:
    import colorlog
    _COLORLOG_AVAILABLE = True
except ImportError:
    _COLORLOG_AVAILABLE = False


def get_logger(name: str) -> logging.Logger:
    """
    Get a named logger. Safe to call multiple times — handlers are only added once.

    Args:
        name: Display name for the logger (e.g. "TrendAgent", "DB")

    Returns:
        Configured logging.Logger instance.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # prevent duplicate root logger output

    plain_fmt = logging.Formatter(
        "[%(asctime)s] [%(name)-15s] [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Console handler (INFO+, colorized if colorlog available) ──────────────
    if _COLORLOG_AVAILABLE:
        color_fmt = colorlog.ColoredFormatter(
            "%(log_color)s[%(asctime)s] [%(name)-15s] [%(levelname)-8s]%(reset)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )
        console = logging.StreamHandler()
        console.setFormatter(color_fmt)
    else:
        console = logging.StreamHandler()
        console.setFormatter(plain_fmt)

    console.setLevel(logging.INFO)

    # ── Main rotating log (DEBUG+) ────────────────────────────────────────────
    main_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "instaagent.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    main_handler.setLevel(logging.DEBUG)
    main_handler.setFormatter(plain_fmt)

    # ── Error-only log (ERROR+) ───────────────────────────────────────────────
    error_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "errors.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(plain_fmt)

    logger.addHandler(console)
    logger.addHandler(main_handler)
    logger.addHandler(error_handler)

    return logger
