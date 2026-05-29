"""Unified trading system — wraps the full pipeline."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("trading.unified")


@dataclass
class TradingSystemConfig:
    paper_mode: bool = True
    max_markets_per_cycle: int = 20
    cycle_sleep_seconds: int = 60


async def run_unified_trading_system(config: Optional[TradingSystemConfig] = None):
    """Run one unified trading cycle."""
    from src.jobs.trade import run_trading_job
    cfg = config or TradingSystemConfig()
    return await run_trading_job()
