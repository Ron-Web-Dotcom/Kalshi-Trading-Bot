"""Phase 7 — risk management: size limits, daily loss, cooldown, exposure."""

import logging
from datetime import datetime, timezone, date, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("trading.risk")


class RiskManager:
    def __init__(self, db=None):
        from src.config.settings import settings
        self.cfg = settings.trading
        self.db = db
        self._last_trade_time: Dict[str, datetime] = {}
        self._daily_loss: float = 0.0
        self._daily_loss_date: Optional[date] = None

    def _reset_daily_if_needed(self):
        today = date.today()
        if self._daily_loss_date != today:
            self._daily_loss = 0.0
            self._daily_loss_date = today

    def check_trade(self, ticker: str, size_dollars: float,
                    current_positions: List[Dict],
                    portfolio_value: float = 1000.0) -> Tuple[bool, str]:
        """
        Returns (allowed, reason). Reason is empty string if allowed.
        """
        self._reset_daily_if_needed()

        # 1. Cooldown
        last = self._last_trade_time.get(ticker)
        if last:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < self.cfg.cooldown_between_trades_seconds:
                remaining = int(self.cfg.cooldown_between_trades_seconds - elapsed)
                return False, f"Cooldown: {remaining}s remaining for {ticker}"

        # 2. Max trade size
        if size_dollars > self.cfg.max_trade_size_dollars:
            return False, f"Trade size ${size_dollars:.2f} exceeds max ${self.cfg.max_trade_size_dollars:.2f}"

        # 3. Daily loss circuit breaker
        max_daily_loss = portfolio_value * (self.cfg.max_daily_loss_pct / 100)
        if self._daily_loss >= max_daily_loss:
            return False, f"Daily loss limit reached: ${self._daily_loss:.2f} >= ${max_daily_loss:.2f}"

        # 4. Max position size as % of portfolio
        max_position = portfolio_value * (self.cfg.max_position_size_pct / 100)
        if size_dollars > max_position:
            return False, f"Position ${size_dollars:.2f} > {self.cfg.max_position_size_pct}% of portfolio"

        # 5. Sector exposure
        if current_positions:
            category_exposure = self._calc_category_exposure(ticker, size_dollars, current_positions)
            max_sector = portfolio_value * (self.cfg.max_sector_exposure_pct / 100)
            if category_exposure > max_sector:
                return False, f"Sector exposure ${category_exposure:.2f} would exceed limit ${max_sector:.2f}"

        return True, ""

    def _calc_category_exposure(self, ticker: str, new_size: float,
                                 positions: List[Dict]) -> float:
        """Sum existing exposure in the same market category."""
        # Use the category field if available; fall back to ticker prefix
        prefix = ticker.split("-")[0] if "-" in ticker else (ticker[:4] if len(ticker) >= 4 else ticker)
        existing = sum(
            p.get("avg_price", 0) * p.get("contracts", 0) / 100
            for p in positions
            if (p.get("category") == self._ticker_category(ticker)
                if p.get("category") else p.get("ticker", "").startswith(prefix))
        )
        return existing + new_size

    @staticmethod
    def _ticker_category(ticker: str) -> str:
        """Extract category prefix from ticker (e.g. 'KXETHD' → 'KXETH')."""
        return ticker.split("-")[0] if "-" in ticker else ticker[:4]

    def record_trade(self, ticker: str, pnl: float = 0.0):
        """Record a completed trade for cooldown and daily loss tracking."""
        self._last_trade_time[ticker] = datetime.now(timezone.utc)
        if pnl < 0:
            self._reset_daily_if_needed()
            self._daily_loss += abs(pnl)

    def clamp_size(self, desired: float) -> float:
        """Clamp trade size to configured min/max."""
        return max(
            self.cfg.min_trade_size_dollars,
            min(desired, self.cfg.max_trade_size_dollars)
        )

    def kelly_size(self, confidence_pct: float, price_cents: float,
                   portfolio_value: float = 1000.0) -> float:
        """
        Fractional Kelly Criterion position size.
        confidence_pct: AI confidence 0-100 (treated as implied win probability).
        price_cents: market price in cents (0-100).
        Returns dollar size, clamped to configured limits.
        """
        if price_cents <= 0 or price_cents >= 100:
            return self.cfg.base_trade_size_dollars
        p = confidence_pct / 100.0
        q = 1.0 - p
        # Decimal odds: $1 at 50¢ pays $1 profit on win (2:1 payout at 100¢)
        b = (100.0 - price_cents) / price_cents
        if b <= 0:
            return self.cfg.base_trade_size_dollars
        kelly = (p * b - q) / b
        if kelly <= 0:
            return self.cfg.min_trade_size_dollars
        # Apply fractional Kelly (configurable, default 0.25)
        fractional = kelly * self.cfg.kelly_fraction
        return self.clamp_size(fractional * portfolio_value)

    def dollars_to_contracts(self, dollars: float, price_cents: float) -> int:
        """Convert dollar amount to number of contracts at given price (cents)."""
        if price_cents <= 0:
            return 0
        return max(1, int(dollars / (price_cents / 100)))
