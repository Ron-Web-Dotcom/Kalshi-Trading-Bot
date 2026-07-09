"""Job: track open positions — mark-to-market, stop-loss, take-profit, settlement."""

import logging
from datetime import datetime, timezone
from typing import Dict, List
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

logger = logging.getLogger("trading.jobs.track")


async def run_tracking(db_manager, risk=None) -> None:
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
    from src.ai.decision import AIDecisionEngine
    from src.data.context_builder import build_market_context
    from src.alerts.discord import DiscordAlerter

    stop_loss_pct    = settings.trading.stop_loss_pct
    take_profit_pct  = settings.trading.take_profit_pct
    enable_reeval    = settings.trading.enable_ai_reeval
    reeval_min_conf  = settings.trading.reeval_min_confidence
    paper            = not settings.trading.live_trading_enabled

    kalshi   = KalshiClient()
    if risk is None:
        risk = RiskManager(db=db_manager)
    ai       = AIDecisionEngine(db=db_manager)
    discord  = DiscordAlerter()
    now      = datetime.now(_ET).isoformat()

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
            reeval    = None  # reset per iteration — prevents cross-position contamination
            ticker    = pos["ticker"]
            side      = pos.get("side", "yes")
            avg_price = float(pos.get("avg_price", 0))   # cents
            contracts = int(pos.get("contracts", 0))
            pos_id    = pos["id"]
            platform  = pos.get("platform", "kalshi")

            # Polymarket positions: fetch current price from the cached markets table
            if platform == "polymarket":
                try:
                    mkt_row = await db_manager.fetchone(
                        "SELECT yes_ask, no_ask, status FROM markets WHERE ticker=?", (ticker,)
                    )
                    if not mkt_row:
                        # Ticker mismatch — fallback to title match
                        pos_title = (pos.get("title") or "").strip()
                        if pos_title:
                            mkt_row = await db_manager.fetchone(
                                "SELECT yes_ask, no_ask, status FROM markets WHERE platform='polymarket' AND title=?",
                                (pos_title,)
                            )
                    if not mkt_row:
                        logger.debug("TRACK SKIP Polymarket %s — not in markets cache (ticker mismatch?)", ticker)
                        continue
                    bid_key   = "yes_ask" if side == "yes" else "no_ask"
                    cur_price = float(mkt_row.get(bid_key, 0) or 0)
                    # Fall back to the other side's price if primary is 0
                    if cur_price == 0:
                        alt_key   = "no_ask" if side == "yes" else "yes_ask"
                        alt_price = float(mkt_row.get(alt_key, 0) or 0)
                        if alt_price > 0:
                            cur_price = 100.0 - alt_price
                    if cur_price == 0:
                        logger.warning("TRACK SKIP Polymarket %s — %s price=0 in cache (stale data?)", ticker, bid_key)
                        continue
                    # cur_price is the side's own price (no_ask for NO, yes_ask for YES)
                    # so PnL formula is the same: profit when our side's price rises
                    pnl = (cur_price - avg_price) * contracts / 100
                    pct_change = ((cur_price - avg_price) / avg_price * 100) if avg_price else 0
                    mkt_status = mkt_row.get("status", "open")
                    if mkt_status in ("resolved", "settled", "finalized"):
                        close_reason = f"resolved:{mkt_status}"
                        # Our side wins when that side's price → 100 at resolution
                        # (yes_ask→100 = YES won; no_ask→100 = NO won)
                        final_price  = 100.0 if cur_price >= 95 else 0.0
                        pnl = (final_price - avg_price) * contracts / 100
                        close_price_used = final_price
                    elif stop_loss_pct > 0 and pct_change <= -stop_loss_pct:
                        close_reason = f"stop_loss:{pct_change:.1f}%"
                        pnl = (cur_price - avg_price) * contracts / 100
                        close_price_used = cur_price
                    elif take_profit_pct > 0 and pct_change >= take_profit_pct:
                        close_reason = f"take_profit:{pct_change:.1f}%"
                        pnl = (cur_price - avg_price) * contracts / 100
                        close_price_used = cur_price

                    if close_reason:
                        await db_manager.execute("""
                            UPDATE positions SET status='closed', current_price=?,
                            pnl=?, closed_at=?, close_reason=? WHERE id=?
                        """, (close_price_used, pnl, now, close_reason, pos_id))
                        pnl_sign = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAK_EVEN")
                        _tl_poly = await db_manager.fetchone(
                            "SELECT id FROM trade_logs WHERE ticker=? AND side=? AND pnl IS NULL "
                            "AND (platform='polymarket') ORDER BY executed_at DESC LIMIT 1",
                            (ticker, side)
                        )
                        if _tl_poly and _tl_poly.get("id"):
                            await db_manager.execute(
                                "UPDATE trade_logs SET pnl=?, result=?, resolved_at=? WHERE id=?",
                                (pnl, pnl_sign, now, _tl_poly["id"])
                            )
                        risk.record_result(ticker, pnl, platform)
                        try:
                            await discord.position_closed(
                                ticker=ticker, side=side,
                                contracts=int(contracts),
                                avg_price=float(avg_price),
                                close_price=float(close_price_used),
                                pnl=float(pnl),
                                reason=close_reason,
                                platform="polymarket",
                            )
                        except Exception as _disc_err:
                            logger.debug("Discord alert failed: %s", _disc_err)
                        closed += 1
                    else:
                        await db_manager.execute(
                            "UPDATE positions SET current_price=?, pnl=? WHERE id=?",
                            (cur_price, pnl, pos_id)
                        )
                        logger.info(
                            "MTM POLY %-28s  %s  cur=%.0f¢  entry=%.0f¢  PnL=%+.2f  (%.1f%%)",
                            ticker, side, cur_price, avg_price, pnl, pct_change,
                        )
                except Exception as e:
                    logger.warning("Poly track error for %s: %s", ticker, e)
                continue

            try:
                resp   = await kalshi.get_market(ticker)
                mkt    = resp.get("market", resp)
                status = mkt.get("status", "open")

                # Current bid = what we can sell for right now (cents)
                bid_key   = "yes_bid" if side == "yes" else "no_bid"
                cur_price = float(mkt.get(bid_key, 0) or 0)
                # Fall back to last_price if bid is 0 (thin/illiquid market)
                if cur_price == 0:
                    cur_price = float(mkt.get("last_price", 0) or 0)
                if cur_price == 0:
                    logger.warning("TRACK SKIP Kalshi %s — %s=0 and no last_price", ticker, bid_key)
                    continue
                # cur_price is the side's own bid (no_bid for NO, yes_bid for YES)
                # so PnL formula is the same for both: profit when our side's price rises
                pnl = (cur_price - avg_price) * contracts / 100
                pct_change = ((cur_price - avg_price) / avg_price * 100) if avg_price else 0

                close_reason = ""
                final_price  = cur_price

                # ── 1. Market resolved ────────────────────────────────────
                if status in ("resolved", "settled", "finalized"):
                    result = mkt.get("result", "")
                    result_lower = (result or "").lower().strip()
                    won    = (side == "yes" and result_lower == "yes") or \
                             (side == "no"  and result_lower == "no")
                    final_price  = 100.0 if won else 0.0
                    pnl          = (final_price - avg_price) * contracts / 100
                    close_reason = f"resolved:{result}"

                # ── 2. Stop-loss ──────────────────────────────────────────
                elif stop_loss_pct > 0 and pct_change <= -stop_loss_pct:
                    close_reason = f"stop_loss:{pct_change:.1f}%"

                # ── 3. Take-profit ────────────────────────────────────────
                elif take_profit_pct > 0 and pct_change >= take_profit_pct:
                    close_reason = f"take_profit:{pct_change:.1f}%"

                # ── 4. AI re-evaluation — opt-out if thesis has broken down ──
                elif enable_reeval:
                    try:
                        fresh_context = await build_market_context(mkt | {"ticker": ticker, "title": mkt.get("title", ticker)})
                        reeval = await ai.evaluate_open_position(pos, mkt | {"ticker": ticker}, fresh_context)
                        if (reeval["verdict"] == "EXIT"
                                and reeval["confidence"] >= reeval_min_conf):
                            close_reason = f"ai_reeval:{reeval['reasoning'][:60]}"
                    except Exception as re_err:
                        logger.debug("Re-eval skipped for %s: %s", ticker, re_err)

                if close_reason:
                    await db_manager.execute("""
                        UPDATE positions
                        SET status='closed', current_price=?, pnl=?,
                            closed_at=?, close_reason=?
                        WHERE id=?
                    """, (final_price, pnl, now, close_reason, pos_id))

                    _tl_track = await db_manager.fetchone(
                        "SELECT id FROM trade_logs WHERE ticker=? AND side=? AND pnl IS NULL "
                        "AND (platform=? OR platform IS NULL) ORDER BY executed_at DESC LIMIT 1",
                        (ticker, side, platform)
                    )
                    if _tl_track and _tl_track.get("id"):
                        await db_manager.execute(
                            "UPDATE trade_logs SET pnl=?, resolved_at=?, result=? WHERE id=?",
                            (pnl, datetime.now(_ET).isoformat(), "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAK_EVEN"), _tl_track["id"])
                        )
                    risk.record_result(ticker, pnl, platform)
                    try:
                        from src.utils.daily_stats import stats as daily_stats
                        if pnl >= 0:
                            daily_stats.record_win()
                        else:
                            daily_stats.record_loss()
                    except Exception:
                        pass
                    closed += 1
                    sign    = "+" if pnl >= 0 else ""
                    trigger = (
                        "RESOLVED"    if close_reason.startswith("resolved")   else
                        "STOP-LOSS"   if close_reason.startswith("stop_loss")  else
                        "TAKE-PROFIT" if close_reason.startswith("take_profit") else
                        "AI OPT-OUT"  if close_reason.startswith("ai_reeval")  else
                        close_reason.upper()
                    )
                    logger.info(
                        "CLOSED  %-28s  %s  %dx  entry=%.0f¢  exit=%.0f¢  "
                        "PnL=%s$%.2f  [%s]",
                        ticker, side, contracts,
                        avg_price, final_price,
                        sign, abs(pnl), trigger,
                    )
                    if close_reason.startswith("ai_reeval") and "reeval" in locals():
                        logger.info("  AI opted out: %s", reeval.get("reasoning", "")[:120])

                    # Discord: position closed (all triggers)
                    market_result = mkt.get("result", "") if close_reason.startswith("resolved") else ""
                    await discord.position_closed(
                        ticker=ticker, side=side, contracts=contracts,
                        entry_cents=avg_price, exit_cents=final_price,
                        pnl=pnl, reason=close_reason, paper=paper,
                        market_result=market_result,
                        market_title=mkt.get("title", "") or mkt.get("question", ""),
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
                    # Discord position update — ONLY when something meaningful changed:
                    #   • Price moved ±5% or more since last alert
                    #   • AI re-eval signals a notable shift (not routine HOLD)
                    last_alerted_price = float(pos.get("last_alerted_price") or avg_price)
                    price_shift = abs(cur_price - last_alerted_price)
                    price_shift_pct = (price_shift / last_alerted_price * 100) if last_alerted_price else 0

                    if price_shift_pct >= 5.0:
                        # Price moved 5%+ — worth telling the user
                        direction = "📈" if cur_price > last_alerted_price else "📉"
                        await discord.ai_reeval_hold(
                            ticker=ticker, side=side, pct_change=pct_change,
                            reasoning=f"{direction} Price moved {price_shift_pct:.1f}% (now {cur_price:.0f}¢, was {last_alerted_price:.0f}¢)",
                            paper=paper,
                        )
                        await db_manager.execute(
                            "UPDATE positions SET last_alerted_price=? WHERE id=?",
                            (cur_price, pos_id)
                        )

            except Exception as e:
                logger.warning("Track error for %s: %s", ticker, e)

        if closed:
            logger.info("Tracking: closed %d of %d position(s)", closed, len(positions))
        else:
            logger.info("Tracking: %d position(s) marked to market", len(positions))

    finally:
        await kalshi.close()
