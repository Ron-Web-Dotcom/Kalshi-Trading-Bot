"""Phase 6 — paper trade execution: simulate orders, track PnL and history."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("trading.paper_trader")

# Kalshi charges ~2% of notional on each side (taker fee).
KALSHI_FEE_PCT = 0.02


class PaperTrader:
    """
    Simulates trade execution without touching real money.
    Tracks PnL, win rate, and full trade history via the database.

    Prices are in CENTS (0–99) throughout.
    """

    def __init__(self, db=None, discord=None, scaler=None, risk=None):
        from src.config.settings import settings
        self.cfg = settings.trading
        self.db = db
        self.discord = discord
        self.scaler = scaler
        self.risk = risk

    async def execute(self, ticker: str, action: str, side: str,
                      price_cents: float, ai_confidence: float = 0.0,
                      ai_reasoning: str = "", signal_source: str = "paper",
                      forced_size: Optional[float] = None,
                      net_ev: Optional[float] = None,
                      true_prob: Optional[float] = None,
                      market_title: str = "",
                      close_time: str = "",
                      ) -> Optional[Dict]:
        """Simulate a trade. Returns trade record dict or None if rejected."""

        # ── Safety gate: price must be 1–99¢ ─────────────────────────────────
        if not (0 < price_cents < 100):
            logger.warning(
                "REJECT %s: price %.1f¢ out of valid range (1–99¢)", ticker, price_cents
            )
            return None

        # ── Sizing: Kelly when we have an AI confidence, else scaler / base ──
        if forced_size is not None:
            size = forced_size
        elif self.risk and ai_confidence > 0:
            kelly_prob = true_prob if true_prob is not None else ai_confidence
            size = self.risk.kelly_size(kelly_prob, price_cents,
                                        portfolio_value=self.cfg.portfolio_value)
            if self.scaler:
                size *= self.scaler.scale_factor
        elif self.scaler:
            size = self.scaler.current_size
        else:
            size = self.cfg.base_trade_size_dollars

        size      = self.risk.clamp_size(size) if self.risk else size
        contracts = self._price_to_contracts(size, price_cents)

        if contracts < 1:
            logger.warning("REJECT %s: computed 0 contracts (size=%.2f price=%.1f¢)",
                           ticker, size, price_cents)
            return None

        notional   = contracts * price_cents / 100
        fee        = notional * KALSHI_FEE_PCT
        total_cost = notional + fee
        now        = datetime.now(timezone.utc).isoformat()

        record = {
            "ticker":        ticker,
            "action":        action,
            "side":          side,
            "contracts":     contracts,
            "price":         price_cents,
            "total_cost":    total_cost,
            "fee":           fee,
            "paper_trade":   1,
            "ai_confidence": ai_confidence,
            "ai_reasoning":  (ai_reasoning or "")[:500],
            "signal_source": signal_source,
            "pnl":           None,
            "executed_at":   now,
        }

        if self.db:
            # Duplicate-position guard: skip if open position on same side already exists
            existing = await self.db.fetchone(
                "SELECT id FROM positions WHERE ticker=? AND side=? AND status='open' AND platform='kalshi'",
                (ticker, side)
            )
            if existing:
                logger.info("SKIP %s %s: open kalshi position exists (id=%s)", ticker, side, existing["id"])
                return None
            if market_title:
                title_existing = await self.db.fetchone(
                    "SELECT id FROM positions WHERE title=? AND side=? AND status='open' AND platform='kalshi'",
                    ((market_title or "")[:200], side)
                )
                if title_existing:
                    logger.info("SKIP %s %s: open kalshi position with same title exists (id=%s)", ticker, side, title_existing["id"])
                    return None

            record_id = await self.db.insert("trade_logs", record)
            record["id"] = record_id

            await self.db.execute("""
                INSERT INTO positions (ticker, side, contracts, avg_price, current_price,
                                       pnl, status, opened_at, platform, title, size_usd, close_time)
                VALUES (?,?,?,?,?,0,'open',?,?,?,?,?)
            """, (ticker, side, contracts, price_cents, price_cents, now,
                  "kalshi",
                  (market_title or "")[:200],
                  round(total_cost, 2),
                  close_time or ""))

            await self.db.insert("paper_signals", {
                "ticker":        ticker,
                "action":        action,
                "side":          side,
                "price":         price_cents,
                "contracts":     contracts,
                "ai_confidence": ai_confidence,
                "ai_reasoning":  (ai_reasoning or "")[:500],
                "arbitrage_pct": None,
                "signal_source": signal_source,
                "outcome":       None,
                "settled":       0,
                "created_at":    now,
            })

        logger.info(
            "◆ PAPER TRADE  %s %s %s | %d contracts @ %.0f¢ | "
            "cost=$%.2f fee=$%.2f | conf=%.0f%% | src=%s",
            action, side.upper(), ticker,
            contracts, price_cents,
            total_cost, fee,
            ai_confidence, signal_source,
        )
        if ai_reasoning:
            logger.info("  Reason: %s", ai_reasoning[:120])

        if self.discord:
            try:
                exp_profit = (contracts * net_ev / 100) if net_ev is not None else None
                await self.discord.trade_executed(
                    ticker=ticker, action=action, side=side,
                    price=price_cents, contracts=contracts,
                    size_dollars=total_cost, pnl=None,
                    ai_confidence=ai_confidence, paper=True,
                    signal_source=signal_source,
                    reasoning=ai_reasoning,
                    net_ev=net_ev,
                    exp_profit=exp_profit,
                    market_title=market_title,
                )
            except Exception as _de:
                logger.debug("Discord alert failed: %s", _de)

        if self.risk:
            self.risk.record_trade(ticker, platform="kalshi")

        return record

    async def get_stats(self) -> Dict:
        if not self.db:
            return {}
        stats = await self.db.fetchone("""
            SELECT
                COUNT(*)                                          AS total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)         AS winning_trades,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)         AS losing_trades,
                SUM(COALESCE(pnl, 0))                             AS total_pnl
            FROM trade_logs WHERE paper_trade=1
        """)
        if not stats:
            return {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_pnl": 0.0}
        total = stats["total_trades"] or 0
        wins  = stats["winning_trades"] or 0
        stats["win_rate"] = (wins / total * 100) if total > 0 else 0.0
        return stats

    async def get_history(self, limit: int = 50) -> List[Dict]:
        if not self.db:
            return []
        return await self.db.fetchall(
            "SELECT * FROM trade_logs WHERE paper_trade=1 ORDER BY executed_at DESC LIMIT ?",
            (limit,)
        )

    def _price_to_contracts(self, dollars: float, price_cents: float) -> int:
        if price_cents <= 0:
            return 0
        return int(dollars / (price_cents / 100))
