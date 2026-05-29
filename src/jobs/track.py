"""Job: track open positions — mark-to-market, stop-loss, take-profit, settlement."""

import logging
from datetime import datetime, timezone
from typing import Dict, List

logger = logging.getLogger("trading.jobs.track")


async def run_tracking(db_manager) -> None:
    """
    For every open position:
      1. Fetch live price from Kalshi
      2. Update mark-to-market PnL
      3. Close if market resolved
      4. Close if stop-loss hit  (STOP_LOSS_PCT in .env, 0 = disabled)
      5. Close if take-profit hit (TAKE_PROFIT_PCT in .env, 0 = disabled)
    """
    from src.clients.kalshi_client import KalshiClient
    from src.risk.manager import RiskManager
    from src.config.settings import settings

    stop_loss_pct   = settings.trading.stop_loss_pct
    take_profit_pct = settings.trading.take_profit_pct

    kalshi = KalshiClient()
    risk   = RiskManager(db=db_manager)
    now    = datetime.now(timezone.utc).isoformat()

    try:
        positions: List[Dict] = await db_manager.fetchall(
            "SELECT * FROM positions WHERE status='open'"
        )
        if not positions:
            logger.debug("No open positions to track")
            return

        logger.info(
            "── Position Tracking (%d open) ─────────────────────────────",
            len(positions)
        )
        closed = 0

        for pos in positions:
            ticker    = pos["ticker"]
            side      = pos.get("side", "yes")
            avg_price = float(pos.get("avg_price", 0))   # cents
            contracts = int(pos.get("contracts", 0))
            pos_id    = pos["id"]

            try:
                resp   = await kalshi.get_market(ticker)
                mkt    = resp.get("market", resp)
                status = mkt.get("status", "open")

                # Current bid = what we can sell for right now (cents)
                bid_key   = "yes_bid" if side == "yes" else "no_bid"
                cur_price = float(mkt.get(bid_key, 0) or 0)
                pnl       = (cur_price - avg_price) * contracts / 100
                pct_change = ((cur_price - avg_price) / avg_price * 100) if avg_price else 0

                close_reason = ""
                final_price  = cur_price

                # ── 1. Market resolved ────────────────────────────────────
                if status in ("resolved", "settled", "finalized"):
                    result = mkt.get("result", "")
                    won    = (side == "yes" and result == "yes") or \
                             (side == "no"  and result == "no")
                    final_price  = 100.0 if won else 0.0
                    pnl          = (final_price - avg_price) * contracts / 100
                    close_reason = f"resolved:{result}"

                # ── 2. Stop-loss ──────────────────────────────────────────
                elif stop_loss_pct > 0 and pct_change <= -stop_loss_pct:
                    close_reason = f"stop_loss:{pct_change:.1f}%"

                # ── 3. Take-profit ────────────────────────────────────────
                elif take_profit_pct > 0 and pct_change >= take_profit_pct:
                    close_reason = f"take_profit:{pct_change:.1f}%"

                if close_reason:
                    await db_manager.execute("""
                        UPDATE positions
                        SET status='closed', current_price=?, pnl=?,
                            closed_at=?, close_reason=?
                        WHERE id=?
                    """, (final_price, pnl, now, close_reason, pos_id))

                    await db_manager.execute(
                        "UPDATE trade_logs SET pnl=? WHERE ticker=? AND pnl IS NULL",
                        (pnl, ticker)
                    )
                    risk.record_trade(ticker, pnl)
                    closed += 1
                    sign = "+" if pnl >= 0 else ""
                    logger.info(
                        "CLOSED  %-28s  %s  %dx  entry=%.0f¢  exit=%.0f¢  "
                        "PnL=%s$%.2f  [%s]",
                        ticker, side, contracts,
                        avg_price, final_price,
                        sign, abs(pnl), close_reason,
                    )
                else:
                    await db_manager.execute(
                        "UPDATE positions SET current_price=?, pnl=? WHERE id=?",
                        (cur_price, pnl, pos_id)
                    )
                    logger.debug(
                        "MTM  %-28s  %s  cur=%.0f¢  entry=%.0f¢  PnL=%+.2f  (%.1f%%)",
                        ticker, side, cur_price, avg_price, pnl, pct_change,
                    )

            except Exception as e:
                logger.warning("Track error for %s: %s", ticker, e)

        if closed:
            logger.info("Tracking: closed %d of %d position(s)", closed, len(positions))
        else:
            logger.info("Tracking: %d position(s) marked to market", len(positions))

    finally:
        await kalshi.close()
