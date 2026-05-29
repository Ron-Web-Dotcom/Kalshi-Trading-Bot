"""Portfolio enforcer — block trades exceeding sector caps."""

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger("trading.portfolio_enforcer")


class PortfolioEnforcer:
    def __init__(self, max_sector_pct: float = 30.0, portfolio_value: float = 1000.0):
        self.max_sector_pct = max_sector_pct
        self.portfolio_value = portfolio_value

    def check(self, category: str, trade_size: float,
               positions: List[Dict]) -> Tuple[bool, str]:
        existing = sum(
            p.get("avg_price", 0) * p.get("contracts", 0) / 100
            for p in positions
            if p.get("category") == category
        )
        max_allowed = self.portfolio_value * (self.max_sector_pct / 100)
        if existing + trade_size > max_allowed:
            return False, f"Sector {category} exposure would exceed {self.max_sector_pct}%"
        return True, ""
