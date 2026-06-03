"""
Polymarket paper trader — simulates trades on Polymarket markets without
placing real orders.

Mirrors PaperTrader (Kalshi) but records platform='polymarket' in the DB
and uses USDC as the currency. In live mode, delegates to PolymarketTradingClient.

Polymarket fee model:
  Unlike Kalshi (2% on notional), Polymarket takes 2% on WINNINGS only.
  Effective fee ≈ (1 - price) × 0.02 × contracts  when position resolves YES.
  We approximate as 1% of notional for sizing purposes (conservative estimate).
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger("trading.poly_paper_trader")

def _poly_fee(price_cents: float, contracts: int) -> float:
    """Polymarket charges 2% of winnings: fee = contracts × (100 - price)/100 × 0.02"""
    return contracts * (100.0 - price_cents) / 100.0 * 0.02


class PolyPaperTrader:
    """Simulates Polymarket trades; records to shared DB with platform='polymarket'."""

    def __init__(self, db=None, discord=None, scaler=None, risk=None):
        from src.config.settings import settings
        self.cfg     = settings.trading
        self.poly_cfg = settings.polymarket
        self.db      = db
        self.discord = discord
        self.scaler  = scaler
        self.risk    = risk

    async def execute(
        self,
        ticker:       str,
        action:       str,
        side:         str,
        price_cents:  float,
        ai_confidence: float = 0.0,
        ai_reasoning: str = "",
        signal_source: str = "poly_ai",
        forced_size:  Optional[float] = None,
        net_ev:       Optional[float] = None,
        true_prob:    Optional[float] = None,
        market_title: str = "",
        poly_token_id: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Simulate a Polymarket trade. Returns trade record or None if rejected.

        In live mode (POLY_LIVE_TRADING=true): delegates to real order placement.
        In paper mode: records simulated fill at current price.
        """
        if not (0 < price_cents < 100):
            logger.warning("POLY REJECT %s: price %.1f¢ out of range", ticker, price_cents)
            return None

        # Size the position
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

        if self.risk:
            size = self.risk.clamp_size(size)

        # Polymarket uses USDC; min order
        size = max(size, self.poly_cfg.min_order_usdc)

        contracts  = int(size / (price_cents / 100))
        if contracts < 1:
            logger.warning("POLY REJECT %s: 0 contracts computed", ticker)
            return None

        notional   = contracts * price_cents / 100
        fee        = _poly_fee(price_cents, contracts)
        total_cost = notional + fee
        now        = datetime.now(timezone.utc).isoformat()

        # Duplicate guard BEFORE placing any live order
        if self.db:
            existing = await self.db.fetchone(
                "SELECT id FROM positions WHERE ticker=? AND side=? AND platform='polymarket' AND status='open'",
                (ticker, side)
            )
            if existing:
                logger.info("POLY SKIP %s %s: open position exists (id=%s)", ticker, side, existing["id"])
                return None

        # Live mode: place real order only after dup check passes
        if self.poly_cfg.live_trading_enabled:
            if not poly_token_id:
                logger.error("POLY LIVE: no token_id for %s — cannot place order", ticker)
                return None
            from src.clients.polymarket_client import PolymarketTradingClient
            client = PolymarketTradingClient()
            resp   = await client.place_order(
                token_id=poly_token_id, side="buy",
                price_cents=price_cents, size_usdc=size,
            )
            await client.close()
            if not resp or resp.get("simulated"):
                logger.error("POLY LIVE order failed for %s — not recording to DB", ticker)
                return None
            live_order_id = resp.get("orderID")
            logger.info("POLY LIVE order placed: %s", live_order_id)
        else:
            live_order_id = None

        # Write to DB regardless of live/paper — live orders need tracking too
        if self.db:
            paper_flag = 0 if self.poly_cfg.live_trading_enabled else 1
            await self.db.insert("trade_logs", {
                "ticker":        ticker,
                "action":        action,
                "side":          side,
                "contracts":     contracts,
                "price":         price_cents,
                "total_cost":    total_cost,
                "fee":           fee,
                "paper_trade":   paper_flag,
                "platform":      "polymarket",
                "ai_confidence": ai_confidence,
                "ai_reasoning":  (ai_reasoning or "")[:500],
                "signal_source": signal_source,
                "pnl":           None,
                "executed_at":   now,
            })
            await self.db.execute("""
                INSERT INTO positions
                  (ticker, side, contracts, avg_price, current_price, pnl,
                   status, platform, poly_token_id, opened_at, title)
                VALUES (?,?,?,?,?,0,'open','polymarket',?,?,?)
            """, (ticker, side, contracts, price_cents, price_cents, poly_token_id, now,
                  (market_title or "")[:200]))

        logger.info(
            "◆ POLY PAPER  %s %s %s | %d contracts @ %.0f¢ | "
            "cost=$%.2f fee=$%.2f | conf=%.0f%% | src=%s",
            action, side.upper(), ticker,
            contracts, price_cents,
            total_cost, fee, ai_confidence, signal_source,
        )
        if ai_reasoning:
            logger.info("  Reason: %s", ai_reasoning[:120])

        record = {
            "ticker": ticker, "action": action, "side": side,
            "contracts": contracts, "price": price_cents,
            "total_cost": total_cost, "fee": fee,
            "platform": "polymarket",
        }
        if live_order_id:
            record["poly_order_id"] = live_order_id

        if self.discord:
            try:
                exp_profit = (contracts * net_ev / 100) if net_ev is not None else None
                await self.discord.trade_executed(
                    ticker=ticker, action=action, side=side,
                    price=price_cents, contracts=contracts,
                    size_dollars=total_cost, pnl=None,
                    ai_confidence=ai_confidence, paper=not self.poly_cfg.live_trading_enabled,
                    signal_source=f"poly:{signal_source}",
                    reasoning=ai_reasoning,
                    net_ev=net_ev,
                    exp_profit=exp_profit,
                    market_title=market_title,
                )
            except Exception:
                pass

        if self.risk:
            self.risk.record_trade(ticker)

        return record
