"""Unified trading system — wraps the full pipeline."""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Any

logger = logging.getLogger("trading.unified")


@dataclass
class TradingSystemConfig:
    paper_mode: bool = True
    max_markets_per_cycle: int = 20
    cycle_sleep_seconds: int = 60


@dataclass
class TradingSystemResults:
    total_positions: int = 0
    total_capital_used: float = 0.0
    capital_efficiency: float = 0.0
    expected_annual_return: float = 0.0
    strategy_results: Dict[str, Any] = field(default_factory=dict)


class UnifiedAdvancedTradingSystem:
    """Compatibility wrapper used by beast_mode_dashboard and beast_mode_bot."""

    def __init__(self, db_manager=None, kalshi_client=None, xai_client=None):
        self.db_manager = db_manager
        self.kalshi_client = kalshi_client
        self.xai_client = xai_client

    def get_system_performance_summary(self) -> Dict[str, Any]:
        return {
            "total_capital": 1000.0,
            "capital_allocation": {
                "market_making": 0.4,
                "directional": 0.5,
                "arbitrage": 0.1,
            },
        }

    async def run(self) -> TradingSystemResults:
        from src.jobs.trade import run_trading_job
        job_result = await run_trading_job(db=self.db_manager)
        return TradingSystemResults(
            total_positions=job_result.total_positions,
            total_capital_used=job_result.total_capital_used,
            capital_efficiency=job_result.capital_efficiency,
            expected_annual_return=job_result.expected_annual_return,
        )


async def run_unified_trading_system(config: Optional[TradingSystemConfig] = None):
    """Run one unified trading cycle."""
    from src.jobs.trade import run_trading_job
    return await run_trading_job()
