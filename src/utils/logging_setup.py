"""Structured logging — color-coded console + rotating daily file."""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Optional

_loggers: dict = {}

# ANSI colors for console (disabled on Windows if not supported)
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_COLORS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[35m",   # magenta
}


class _ColorFormatter(logging.Formatter):
    """Console formatter with color-coded level labels."""

    _use_color: bool = sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"

    def format(self, record: logging.LogRecord) -> str:
        levelname = record.levelname
        if self._use_color:
            color = _COLORS.get(levelname, "")
            record.levelname = f"{color}{_BOLD}{levelname:<8}{_RESET}"
        else:
            record.levelname = f"{levelname:<8}"
        return super().format(record)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure root logger with clean console + file handlers."""
    from src.config.settings import settings

    level   = getattr(logging, log_level.upper(), logging.INFO)
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = []

    # ── Console ──────────────────────────────────────────────────────────────
    if settings.logging.enable_console_logging:
        fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ch  = logging.StreamHandler(sys.stdout)
        ch.setLevel(level)
        ch.setFormatter(_ColorFormatter(fmt, datefmt))
        handlers.append(ch)

    # ── File (plain, no ANSI) ─────────────────────────────────────────────────
    if settings.logging.enable_file_logging:
        os.makedirs(settings.logging.log_dir, exist_ok=True)
        log_file = os.path.join(
            settings.logging.log_dir,
            f"bot_{datetime.now().strftime('%Y%m%d')}.log",
        )
        fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
        fh  = RotatingFileHandler(
            log_file,
            maxBytes=50 * 1024 * 1024,  # 50 MB per file
            backupCount=14,              # keep 14 rotated files (~700 MB max)
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(fmt, datefmt))
        handlers.append(fh)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "anthropic", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.basicConfig(level=level, handlers=handlers, force=True)


def get_trading_logger(name: str) -> logging.Logger:
    if name not in _loggers:
        _loggers[name] = logging.getLogger(f"trading.{name}")
    return _loggers[name]
