"""Logging setup — structured console + file logging."""

import logging
import os
import sys
from datetime import datetime
from typing import Optional

_loggers: dict = {}


def setup_logging(log_level: str = "INFO") -> None:
    from src.config.settings import settings

    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = []

    if settings.logging.enable_console_logging:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(level)
        ch.setFormatter(logging.Formatter(fmt, datefmt))
        handlers.append(ch)

    if settings.logging.enable_file_logging:
        os.makedirs(settings.logging.log_dir, exist_ok=True)
        log_file = os.path.join(
            settings.logging.log_dir,
            f"bot_{datetime.now().strftime('%Y%m%d')}.log"
        )
        fh = logging.FileHandler(log_file)
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(fmt, datefmt))
        handlers.append(fh)

    logging.basicConfig(level=level, handlers=handlers, force=True)


def get_trading_logger(name: str) -> logging.Logger:
    if name not in _loggers:
        _loggers[name] = logging.getLogger(f"trading.{name}")
    return _loggers[name]
