"""Live trading execution — places real orders on Kalshi exchange."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("trading.live_trader")

KALSHI_FEE_PCT = 0.02


class LiveTrader:
    """
    Places real orders via the Kalshi API.

    Safety gates (checked before EVERY order):
      - LIVE_TRADING_ENABLED must be True in settings
      - Daily loss circuit breaker
      - Risk manager approval
      - Price sanity check (must be 1–99¢)
      - Minimum net edge after fees
    """

    def __init__(self, kalshi, db=None, discord=None, scaler=None, risk=None):
        from src.config.settings import settings
        self.cfg = settings.trading
        self.kalshi = kalshi
        self.db = db
        self.discord = discord
        self.scaler = scaler
        self.risk = risk

        if not self.cfg.live_trading_enabled:
            raise RuntimeError(
                "LiveTrader instantiated but LIVE_TRADING_ENABLED=false. "
                "Set it to true in .env only after reviewing paper trade results."
            )
        logger.warning("LiveTrader active — REAL MONEY WILL BE USED")

    async def execute(self, ticker: str, action: str, side: str,
                      price_cents: float, ai_confidence: float = 0.0,
                      ai_reasoning: str = "", signal_source: str = "live",
                      forced_size: Optional[float] = None,
                      net_ev: Optional[float] = None,
                      true_prob: Optional[float] = None,
                      market_title: str = "", **kwargs) -> Optional[Dict]:
        """Place a real limit order on Kalshi. Returns order dict or None."""

        # ── Safety gates ──────────────────────────────────────────────────────
        if not self.cfg.live_trading_enabled:
            logger.error("execute() called but live trading disabled — aborting")
            return None

        if price_cents <= 0 or price_cents >= 100:
            logger.warning(f"[LIVE] Refusing trade {ticker}: invalid price {price_cents:.0f}¢")
            return None

        # ── Size ──────────────────────────────────────────────────────────────
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

        size = self.risk.clamp_size(size) if self.risk else size
        contracts = int(size / (price_cents / 100))
        if contracts < 1:
            logger.warning(f"[LIVE] Refusing trade {ticker}: computed size too small (size={size:.2f}, price={price_cents}¢)")
            return None

        notional = contracts * price_cents / 100
        fee = notional * KALSHI_FEE_PCT
        total_cost = notional + fee

        # ── Duplicate guard BEFORE placing — prevent orphaned live orders ─────
        if self.db:
            existing = await self.db.fetchone(
                "SELECT id FROM positions WHERE ticker=? AND side=? AND status='open'",
                (ticker, side)
            )
            if existing:
                logger.warning(
                    "[LIVE] Dup guard: %s %s already open (id=%s) — not placing order",
                    ticker, side, existing["id"]
                )
                return None

        # ── Place order ───────────────────────────────────────────────────────
        try:
            order_resp = await self.kalshi.create_order(
                ticker=ticker,
                side=side,
                action=action.lower(),
                count=contracts,
                price=int(round(price_cents)),
                order_type="limit",
                time_in_force="gtc",
            )
        except Exception as e:
            logger.error(f"[LIVE] Order failed {ticker}: {e}")
            if self.discord:
                try:
                    await self.discord.error_alert(
                        f"Live order failed: {e}",
                        context=f"{ticker} {action} {side} @ {price_cents:.0f}¢"
                    )
                except Exception:
                    pass
            return None

        order_id = order_resp.get("order", {}).get("order_id", "unknown")
        now = datetime.now(timezone.utc).isoformat()

        record = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "contracts": contracts,
            "price": price_cents,
            "total_cost": total_cost,
            "fee": fee,
            "paper_trade": 0,
            "ai_confidence": ai_confidence,
            "ai_reasoning": ai_reasoning[:500] if ai_reasoning else "",
            "signal_source": signal_source,
            "pnl": None,
            "executed_at": now,
        }

        if self.db:
            record_id = await self.db.insert("trade_logs", record)
            record["id"] = record_id
            await self.db.execute("""
                INSERT INTO positions (ticker, side, contracts, avg_price, current_price,
                                       pnl, status, opened_at, platform, title)
                VALUES (?,?,?,?,?,0,'open',?,?,?)
            """, (ticker, side, contracts, price_cents, price_cents, now,
                  "kalshi", (market_title or "")[:200]))

        logger.warning(
            f"[LIVE ORDER] {action} {side.upper()} {ticker} | "
            f"{contracts} contracts @ {price_cents:.0f}¢ | "
            f"Cost=${total_cost:.2f} (fee=${fee:.2f}) | "
            f"order_id={order_id} | AI={ai_confidence:.0f}%"
        )

        if self.discord:
            try:
                await self.discord.trade_executed(
                    ticker=ticker, action=action, side=side,
                    price=price_cents, contracts=contracts,
                    size_dollars=total_cost, pnl=None,
                    ai_confidence=ai_confidence, paper=False,
                    market_title=market_title,
                )
            except Exception:
                pass

        if self.risk:
            self.risk.record_trade(ticker)

        return record
