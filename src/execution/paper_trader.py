"""Phase 6 — paper trade execution: simulate orders, track PnL and history."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("trading.paper_trader")


class PaperTrader:
    """
    Simulates trade execution without touching real money.
    Tracks PnL, win rate, and full trade history via the database.
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
                      forced_size: Optional[float] = None) -> Optional[Dict]:
        """
        Simulate a trade. Returns trade record dict or None if rejected.
        """
        # Determine trade size
        if forced_size is not None:
            size = forced_size
        elif self.scaler:
            size = self.scaler.current_size
        else:
            size = self.cfg.base_trade_size_dollars

        size = self.risk.clamp_size(size) if self.risk else size
        contracts = self._price_to_contracts(size, price_cents)
        if contracts < 1:
            logger.warning(f"Skipping {ticker}: computed 0 contracts")
            return None

        total_cost = contracts * price_cents / 100
        now = datetime.now(timezone.utc).isoformat()

        record = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "contracts": contracts,
            "price": price_cents,
            "total_cost": total_cost,
            "paper_trade": 1,
            "ai_confidence": ai_confidence,
            "ai_reasoning": ai_reasoning[:500] if ai_reasoning else "",
            "signal_source": signal_source,
            "pnl": None,
            "executed_at": now,
        }

        if self.db:
            record_id = await self.db.insert("trade_logs", record)
            record["id"] = record_id

            # Update or create position
            await self.db.execute("""
                INSERT INTO positions (ticker, side, contracts, avg_price, current_price,
                                       pnl, status, opened_at)
                VALUES (?,?,?,?,?,0,'open',?)
            """, (ticker, side, contracts, price_cents, price_cents, now))

            # Log to paper_signals for tracking
            await self.db.insert("paper_signals", {
                "ticker": ticker,
                "action": action,
                "side": side,
                "price": price_cents,
                "contracts": contracts,
                "ai_confidence": ai_confidence,
                "ai_reasoning": ai_reasoning[:500] if ai_reasoning else "",
                "arbitrage_pct": None,
                "signal_source": signal_source,
                "outcome": None,
                "settled": 0,
                "created_at": now,
            })

        logger.info(
            f"[PAPER TRADE] {action} {side.upper()} {ticker} | "
            f"{contracts} contracts @ {price_cents:.0f}¢ | "
            f"Cost=${total_cost:.2f} | AI={ai_confidence:.0f}%"
        )

        if self.discord:
            try:
                await self.discord.trade_executed(
                    ticker=ticker, action=action, side=side,
                    price=price_cents, contracts=contracts,
                    size_dollars=total_cost, pnl=None,
                    ai_confidence=ai_confidence, paper=True,
                )
            except Exception:
                pass

        if self.risk:
            self.risk.record_trade(ticker)

        return record

    async def get_stats(self) -> Dict:
        if not self.db:
            return {}
        stats = await self.db.fetchone("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                SUM(COALESCE(pnl, 0)) as total_pnl
            FROM trade_logs WHERE paper_trade=1
        """)
        if not stats:
            return {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_pnl": 0.0}
        total = stats["total_trades"] or 0
        wins = stats["winning_trades"] or 0
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
        return max(1, int(dollars / (price_cents / 100)))
