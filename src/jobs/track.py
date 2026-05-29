"""Job: track open positions and apply exit logic."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("trading.jobs.track")


async def run_tracking(db_manager) -> None:
    """Check open positions against current market prices and close if needed."""
    from src.clients.kalshi_client import KalshiClient
    from src.risk.manager import RiskManager

    kalshi = KalshiClient()
    risk = RiskManager(db=db_manager)
    now = datetime.now(timezone.utc).isoformat()

    try:
        positions = await db_manager.fetchall(
            "SELECT * FROM positions WHERE status='open'"
        )
        if not positions:
            return

        for pos in positions:
            ticker = pos["ticker"]
            try:
                market = await kalshi.get_market(ticker)
                mkt = market.get("market", market)
                status = mkt.get("status", "open")

                current_price = 0.0
                side = pos.get("side", "yes")
                if side == "yes":
                    current_price = mkt.get("yes_bid", 0)
                else:
                    current_price = mkt.get("no_bid", 0)

                avg_price = pos.get("avg_price", 0)
                contracts = pos.get("contracts", 0)
                pnl = (current_price - avg_price) * contracts / 100

                # Close if market resolved
                if status in ("resolved", "settled", "finalized"):
                    result = mkt.get("result", "")
                    final_price = 100.0 if (
                        (side == "yes" and result == "yes") or
                        (side == "no" and result == "no")
                    ) else 0.0
                    pnl = (final_price - avg_price) * contracts / 100
                    close_reason = f"resolved:{result}"

                    await db_manager.execute("""
                        UPDATE positions SET status='closed', current_price=?,
                        pnl=?, closed_at=?, close_reason=? WHERE id=?
                    """, (final_price, pnl, now, close_reason, pos["id"]))

                    await db_manager.execute(
                        "UPDATE trade_logs SET pnl=? WHERE ticker=? AND pnl IS NULL",
                        (pnl, ticker)
                    )
                    risk.record_trade(ticker, pnl)
                    logger.info(f"[CLOSED] {ticker} | PnL=${pnl:+.2f} | {close_reason}")
                else:
                    # Update mark-to-market
                    await db_manager.execute(
                        "UPDATE positions SET current_price=?, pnl=? WHERE id=?",
                        (current_price, pnl, pos["id"])
                    )

            except Exception as e:
                logger.warning(f"Track error for {ticker}: {e}")

    finally:
        await kalshi.close()
