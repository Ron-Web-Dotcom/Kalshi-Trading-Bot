"""Phase 7 — risk management: size limits, daily loss, cooldown, exposure."""

import logging
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

logger = logging.getLogger("trading.risk")


class RiskManager:
    def __init__(self, db=None):
        from src.config.settings import settings
        self.cfg = settings.trading
        self.db = db
        self._last_trade_time: Dict[tuple, datetime] = {}
        self._daily_loss: float = 0.0
        self._daily_loss_date: Optional[date] = None

    def _reset_daily_if_needed(self):
        today = datetime.now(_ET).date()
        if self._daily_loss_date != today:
            self._daily_loss = 0.0
            self._daily_loss_date = today

    async def get_daily_loss_from_db(self) -> float:
        """Read today's realised losses from DB so the circuit breaker survives restarts."""
        if not self.db:
            return self._daily_loss
        try:
            from src.config.settings import settings
            paper_flag = 0 if settings.trading.live_trading_enabled else 1
            today = datetime.now(_ET).date().isoformat()
            row = await self.db.fetchone(
                "SELECT COALESCE(SUM(ABS(pnl)),0) AS loss FROM trade_logs "
                "WHERE pnl < 0 AND paper_trade=? AND resolved_at >= ?",
                (paper_flag, today + "T00:00:00",)
            )
            return float((row or {}).get("loss", 0))
        except Exception:
            return self._daily_loss

    def check_trade(self, ticker: str, size_dollars: float,
                    current_positions: List[Dict],
                    portfolio_value: float = 1000.0,
                    daily_loss_override: float = 0.0,
                    platform: str = "kalshi") -> Tuple[bool, str]:
        """
        Returns (allowed, reason). Reason is empty string if allowed.
        Pass daily_loss_override from DB query to make the circuit breaker
        survive process restarts.
        """
        self._reset_daily_if_needed()

        # 1. Cooldown — keyed by (ticker, platform) so Kalshi/Poly don't block each other
        last = self._last_trade_time.get((ticker, platform))
        if last:
            elapsed = (datetime.now(_ET) - last).total_seconds()
            if elapsed < self.cfg.cooldown_between_trades_seconds:
                remaining = int(self.cfg.cooldown_between_trades_seconds - elapsed)
                return False, f"Cooldown: {remaining}s remaining for {ticker}"

        # 2. Max trade size
        if size_dollars > self.cfg.max_trade_size_dollars:
            return False, f"Trade size ${size_dollars:.2f} exceeds max ${self.cfg.max_trade_size_dollars:.2f}"

        # 3. Daily loss circuit breaker — use DB value if provided (survives restarts)
        effective_daily_loss = max(self._daily_loss, daily_loss_override)
        max_daily_loss = portfolio_value * (self.cfg.max_daily_loss_pct / 100)
        if effective_daily_loss >= max_daily_loss:
            return False, f"Daily loss limit reached: ${effective_daily_loss:.2f} >= ${max_daily_loss:.2f}"

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
        """Extract category prefix from ticker (e.g. 'KXETHD-24' → 'KXETHD')."""
        return ticker.split("-")[0] if "-" in ticker else ticker

    def record_trade(self, ticker: str, pnl: float = 0.0, platform: str = "kalshi"):
        """Record a completed trade for cooldown tracking."""
        now = datetime.now(_ET)
        self._last_trade_time[(ticker, platform)] = now
        # Prune stale entries to prevent unbounded growth over thousands of tickers
        cutoff = now - timedelta(seconds=self.cfg.cooldown_between_trades_seconds * 2)
        self._last_trade_time = {k: v for k, v in self._last_trade_time.items() if v > cutoff}

    def record_result(self, ticker: str, pnl: float, platform: str = "kalshi"):
        """Record a resolved trade result for daily loss circuit breaker."""
        if pnl < 0:
            self._reset_daily_if_needed()
            self._daily_loss += abs(pnl)

    def clamp_size(self, desired: float) -> float:
        """Clamp trade size to configured min/max."""
        return max(
            self.cfg.min_trade_size_dollars,
            min(desired, self.cfg.max_trade_size_dollars)
        )

    def kelly_size(self, win_prob_pct: float, price_cents: float,
                   portfolio_value: float = 1000.0) -> float:
        """
        Fractional Kelly Criterion position size.
        win_prob_pct: AI's estimated TRUE win probability 0-100 (not confidence).
                      Pass decision.true_prob when available; fall back to confidence.
        price_cents: market price in cents (0-100).
        Returns dollar size, clamped to configured limits.
        """
        if price_cents <= 0 or price_cents >= 100:
            return self.cfg.base_trade_size_dollars
        p = min(max(win_prob_pct / 100.0, 0.01), 0.99)
        q = 1.0 - p
        # Net payout after Kalshi 2% fee: win (100-price)*0.98, lose price
        b = (100.0 - price_cents) * 0.98 / price_cents
        if b <= 0:
            return self.cfg.base_trade_size_dollars
        kelly = (p * b - q) / b
        if kelly <= 0:
            return self.cfg.min_trade_size_dollars
        fractional = kelly * self.cfg.kelly_fraction
        return self.clamp_size(fractional * portfolio_value)

    async def check_daily_loss_lockout(self, db) -> tuple:
        """
        Returns (locked_out, reason).
        locked_out=True means stop trading.

        Checks:
          1. Sum of PnL from positions closed today — if loss > max_daily_loss_usd → lockout
          2. Last N closed positions ordered by closed_at DESC — if all N are losses → lockout
        """
        from src.config.settings import settings
        max_loss = settings.trading.max_daily_loss_usd
        max_streak = settings.trading.max_consecutive_losses

        if db is None:
            return False, ""

        # Check 1: daily loss total — use trade_logs directly to avoid JOIN fan-out
        try:
            paper_flag = 0 if settings.trading.live_trading_enabled else 1
            today = datetime.now(_ET).date().isoformat()
            row = await db.fetchone(
                "SELECT COALESCE(SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END), 0) AS daily_loss "
                "FROM trade_logs "
                "WHERE paper_trade = ? AND resolved_at >= ? AND pnl IS NOT NULL",
                (paper_flag, today + "T00:00:00"),
            )
            daily_loss = float((row or {}).get("daily_loss", 0) or 0)
            if daily_loss >= max_loss:
                return (
                    True,
                    f"Daily loss limit ${max_loss:.2f} reached (lost ${daily_loss:.2f}) — locked out until midnight",
                )
        except Exception as e:
            logger.warning("check_daily_loss_lockout: error checking daily loss: %s", e)

        # Check 2: consecutive loss streak
        # Streak resets at 3am ET (bot sleep/rest period) — fresh start each morning
        try:
            from datetime import timedelta as _td
            now_et = datetime.now(_ET)
            today_3am = now_et.replace(hour=3, minute=0, second=0, microsecond=0)
            # Before 3am: use yesterday's 3am as the window start (today's 3am is in the future)
            if now_et < today_3am:
                today_3am -= _td(days=1)
            streak_since = today_3am.isoformat()
            rows = await db.fetchall(
                "SELECT result FROM trade_logs WHERE result IN ('WIN','LOSS') "
                "AND resolved_at >= ? "
                "ORDER BY resolved_at DESC LIMIT ?",
                (streak_since, max_streak),
            )
            if rows and len(rows) >= max_streak:
                all_losses = all(r.get("result") == 'LOSS' for r in rows)
                if all_losses:
                    return (
                        True,
                        f"{max_streak} consecutive losses — cooling down",
                    )
        except Exception as e:
            logger.warning("check_daily_loss_lockout: error checking streak: %s", e)

        return False, ""

    def dollars_to_contracts(self, dollars: float, price_cents: float) -> int:
        """Convert dollar amount to number of contracts at given price (cents)."""
        if price_cents <= 0:
            return 0
        return int(dollars / (price_cents / 100))
