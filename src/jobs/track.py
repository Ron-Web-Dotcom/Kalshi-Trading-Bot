"""Job: track open positions — mark-to-market, stop-loss, take-profit, settlement."""

import logging
import time
from datetime import datetime
from typing import Dict, List
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

def _et_now() -> str:
    """Eastern Time ISO timestamp for all DB writes."""
    return datetime.now(_ET).strftime("%Y-%m-%dT%H:%M:%S")

logger = logging.getLogger("trading.jobs.track")

# Two-stage opt-out tracker: pos_id → unix timestamp when AI said HOLD on a losing position.
# If the position is STILL losing on the next re-eval pass → cash out regardless.
_reeval_hold_since: Dict[int, float] = {}
_REEVAL_PATIENCE_SECS = 1800  # 30 min: if still losing after HOLD, cash out


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
    now      = _et_now()

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
        _batch_wins:  List[Dict] = []
        _batch_losses: List[Dict] = []
        _batch_exits:  List[Dict] = []

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
                    close_reason     = ""   # must initialize before any conditional sets it
                    close_price_used = 0.0
                    mkt_row = await db_manager.fetchone(
                        "SELECT yes_ask, no_ask, status, close_time FROM markets WHERE ticker=?", (ticker,)
                    )
                    if not mkt_row:
                        # Ticker mismatch — fallback to title match
                        pos_title = (pos.get("title") or "").strip()
                        if pos_title:
                            mkt_row = await db_manager.fetchone(
                                "SELECT yes_ask, no_ask, status, close_time FROM markets WHERE platform='polymarket' AND title=?",
                                (pos_title,)
                            )

                    # If close_time has passed, hit the live API directly — resolved markets
                    # disappear from the active feed so the DB cache never updates to "resolved"
                    ct_str = (mkt_row or {}).get("close_time", "") or pos.get("close_time", "")
                    _past_close = False
                    if ct_str:
                        try:
                            from datetime import timezone as _tz
                            _ct = datetime.fromisoformat(str(ct_str).replace("Z", "+00:00"))
                            if _ct.tzinfo is None:
                                _ct = _ct.replace(tzinfo=_tz.utc).astimezone(_ET)
                            _past_close = datetime.now(_ET) > _ct
                        except Exception:
                            pass

                    if _past_close:
                        # Market close_time passed — check live API for real resolution
                        try:
                            from src.clients.polymarket_client import PolymarketTradingClient as _PTC
                            _pc = _PTC()
                            yes_token = pos.get("poly_yes_token") or pos.get("_yes_token") or ticker
                            live_mkt = await _pc.get_market_by_token(yes_token)
                            await _pc.close()
                            if live_mkt:
                                # Merge live data into mkt_row
                                mkt_row = dict(mkt_row or {})
                                mkt_row["yes_ask"] = live_mkt.get("yes_ask", mkt_row.get("yes_ask", 0))
                                mkt_row["no_ask"]  = live_mkt.get("no_ask",  mkt_row.get("no_ask",  0))
                                if live_mkt.get("status") in ("resolved", "settled", "finalized", "closed"):
                                    mkt_row["status"] = live_mkt["status"]
                                    # Update DB so we don't re-fetch next cycle
                                    await db_manager.execute(
                                        "UPDATE markets SET status=?, yes_ask=?, no_ask=? WHERE ticker=?",
                                        (live_mkt["status"], mkt_row["yes_ask"], mkt_row["no_ask"], ticker)
                                    )
                                logger.info("POLY LIVE CHECK %s → status=%s yes=%.0f¢",
                                            ticker[:40], mkt_row.get("status","?"), mkt_row.get("yes_ask",0))
                        except Exception as _live_err:
                            logger.debug("Poly live check failed for %s: %s", ticker, _live_err)

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
                    if mkt_status in ("resolved", "settled", "finalized", "closed"):
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
                    elif enable_reeval and pct_change <= -10:
                        # Two-stage opt-out for Polymarket — same logic as Kalshi
                        try:
                            patience_expired = (
                                pos_id in _reeval_hold_since
                                and (time.time() - _reeval_hold_since[pos_id]) >= _REEVAL_PATIENCE_SECS
                            )
                            poly_mkt_ctx = {
                                "ticker": ticker, "title": pos.get("title", ticker),
                                "yes_ask": cur_price, "platform": "polymarket",
                            }
                            fresh_ctx = await build_market_context(poly_mkt_ctx)
                            reeval = await ai.evaluate_open_position(pos, poly_mkt_ctx, fresh_ctx)
                            if reeval["verdict"] == "EXIT" and reeval["confidence"] >= reeval_min_conf:
                                close_reason = f"ai_reeval:{reeval['reasoning'][:60]}"
                                pnl = (cur_price - avg_price) * contracts / 100
                                close_price_used = cur_price
                                _reeval_hold_since.pop(pos_id, None)
                            elif patience_expired:
                                close_reason = f"ai_reeval:no improvement after hold — cashing out ({pct_change:.1f}%)"
                                pnl = (cur_price - avg_price) * contracts / 100
                                close_price_used = cur_price
                                _reeval_hold_since.pop(pos_id, None)
                                logger.info("CASHOUT POLY %s — still down %.1f%% after hold period", ticker, pct_change)
                            else:
                                if pos_id not in _reeval_hold_since:
                                    _reeval_hold_since[pos_id] = time.time()
                                    logger.info("WATCH POLY %s — AI says hold, watching for %.0f min", ticker, _REEVAL_PATIENCE_SECS / 60)
                        except Exception as _re_err:
                            logger.debug("Poly re-eval skipped for %s: %s", ticker, _re_err)

                    if close_reason:
                        _reeval_hold_since.pop(pos_id, None)  # clean up watcher on any close
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
                        _batch_item = {
                            "title": (pos.get("title") or ticker)[:50],
                            "ticker": ticker, "side": side.upper(),
                            "platform": "polymarket",
                            "entry": float(avg_price), "exit": float(close_price_used),
                            "pnl": float(pnl), "contracts": int(contracts),
                            "reason": close_reason,
                        }
                        if close_reason.startswith("ai_reeval"):
                            _batch_exits.append(_batch_item)
                        elif pnl >= 0:
                            _batch_wins.append(_batch_item)
                        else:
                            _batch_losses.append(_batch_item)
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

                # ── 4. AI re-evaluation — two-stage opt-out ──────────────────
                # Stage 1: down 10% → AI decides EXIT now or HOLD and watch
                # Stage 2: still losing after HOLD → cash out (patience expired)
                elif enable_reeval and pct_change <= -10:
                    try:
                        patience_expired = (
                            pos_id in _reeval_hold_since
                            and (time.time() - _reeval_hold_since[pos_id]) >= _REEVAL_PATIENCE_SECS
                        )
                        fresh_context = await build_market_context(mkt | {"ticker": ticker, "title": mkt.get("title", ticker)})
                        reeval = await ai.evaluate_open_position(pos, mkt | {"ticker": ticker}, fresh_context)
                        if reeval["verdict"] == "EXIT" and reeval["confidence"] >= reeval_min_conf:
                            close_reason = f"ai_reeval:{reeval['reasoning'][:60]}"
                            _reeval_hold_since.pop(pos_id, None)
                        elif patience_expired:
                            # Held long enough — cash out, thesis hasn't recovered
                            close_reason = f"ai_reeval:no improvement after hold — cashing out ({pct_change:.1f}%)"
                            _reeval_hold_since.pop(pos_id, None)
                            logger.info("CASHOUT %s — still down %.1f%% after hold period", ticker, pct_change)
                        else:
                            # AI says HOLD — start the patience timer if not already running
                            if pos_id not in _reeval_hold_since:
                                _reeval_hold_since[pos_id] = time.time()
                                logger.info("WATCH %s — AI says hold, watching for %.0f min", ticker, _REEVAL_PATIENCE_SECS / 60)
                    except Exception as re_err:
                        logger.debug("Re-eval skipped for %s: %s", ticker, re_err)

                if close_reason:
                    _reeval_hold_since.pop(pos_id, None)  # clean up watcher on any close
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
                            (pnl, _et_now(), "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAK_EVEN"), _tl_track["id"])
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

                    # Batch for end-of-cycle grouped Discord summary
                    _mkt_title = mkt.get("title", "") or mkt.get("question", "") or ticker
                    _batch_item = {
                        "title": _mkt_title[:50], "ticker": ticker,
                        "side": side.upper(), "platform": "kalshi",
                        "entry": float(avg_price), "exit": float(final_price),
                        "pnl": float(pnl), "contracts": int(contracts),
                        "reason": close_reason,
                    }
                    if close_reason.startswith("ai_reeval"):
                        _batch_exits.append(_batch_item)
                    elif pnl >= 0:
                        _batch_wins.append(_batch_item)
                    else:
                        _batch_losses.append(_batch_item)
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
            # One grouped Discord embed for all closes this cycle
            if _batch_wins or _batch_losses or _batch_exits:
                try:
                    await discord.live_results_summary(
                        wins=_batch_wins,
                        losses=_batch_losses,
                        exits=_batch_exits,
                        mode="📝 PAPER" if paper else "💰 LIVE",
                    )
                except Exception as _flush_err:
                    logger.debug("Batch Discord flush failed: %s", _flush_err)
        else:
            logger.info("Tracking: %d position(s) marked to market", len(positions))

    finally:
        await kalshi.close()
