"""
Live Market Manager — always-on background loop.

Maintains up to MAX_LIVE_POSITIONS concurrent in-play trades across ALL categories
on both Kalshi and Polymarket. Every SCAN_INTERVAL seconds:

  1. Sync live slots with DB (positions opened via signal_source='live_scan')
  2. Check each live position:
       - Market resolved / close_time passed → close slot, scan for replacement
       - Price moved against us by > STOP_LOSS_PCT → exit, Discord alert, scan for replacement
  3. If slots < MAX_LIVE_POSITIONS → scan all live markets (every category), AI ranks them,
     enter the best ones to fill empty slots
  4. Send a single Discord embed for new entries and a separate one for exits

Key design choices:
  - ALL categories scanned (crypto, politics, sports, weather, economics, pop culture…)
  - Minimum confidence to enter: same as bot-wide MIN_AI_CONFIDENCE setting
  - Relaxed profit gate: 2% ROI / $0.50 absolute (live markets resolve fast)
  - Stop-loss per live position: 40% loss on current price vs entry
  - On bad exit: immediately find replacement to keep 3 slots full
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger("trading.live_manager")

MAX_LIVE_POSITIONS   = 3     # keep exactly this many in-play trades at a time
SCAN_INTERVAL        = 300   # seconds between manager cycles (5 minutes)
LIVE_WINDOW_HOURS    = 3.0   # markets closing within this window qualify as "live"
STOP_LOSS_PCT        = 40.0  # exit if current_price dropped this % from entry (YES side)
AI_EVAL_N            = 8     # how many pre-scored markets to send to AI per fill cycle
MIN_ROI_PCT          = 2.0   # minimum ROI% for live trades (lower than regular)
MIN_ABS_USD          = 0.50  # minimum absolute expected profit per live trade

# in-memory slot registry — ticker → slot dict
_live_slots: Dict[str, Dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _hours_left(close_time: str) -> Optional[float]:
    dt = _parse_dt(close_time)
    if not dt:
        return None
    return (dt - _now_utc()).total_seconds() / 3600


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _load_live_positions(db) -> List[Dict]:
    """
    Return all open positions that were entered by the live scanner.
    Joins positions with trade_logs on ticker to find signal_source='live_scan'.
    """
    try:
        rows = await db.fetchall("""
            SELECT p.ticker, p.side, p.avg_price, p.current_price,
                   p.contracts, p.platform, p.title, p.opened_at
            FROM positions p
            WHERE p.status = 'open'
              AND p.ticker IN (
                  SELECT DISTINCT ticker FROM trade_logs
                  WHERE signal_source = 'live_scan'
              )
        """)
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.debug("_load_live_positions error: %s", e)
        return []


async def _close_position(db, ticker: str, reason: str) -> None:
    """Mark a position closed in the DB."""
    try:
        await db.execute("""
            UPDATE positions
               SET status='closed', closed_at=?, close_reason=?
             WHERE ticker=? AND status='open'
        """, (_now_utc().isoformat(), reason[:200], ticker))
    except Exception as e:
        logger.debug("_close_position error %s: %s", ticker, e)


# ── Price fetching ────────────────────────────────────────────────────────────

async def _fetch_kalshi_price(ticker: str, kalshi) -> Optional[float]:
    try:
        data = await kalshi._request("GET", f"/markets/{ticker}")
        market = data.get("market") or data
        yes_ask = market.get("yes_ask") or market.get("last_price") or 0
        return float(yes_ask)
    except Exception:
        return None


async def _fetch_poly_price(ticker: str, db) -> Optional[float]:
    """Read last known price from DB markets table (updated by trade job)."""
    try:
        row = await db.fetchone(
            "SELECT yes_ask FROM markets WHERE ticker=?", (ticker,)
        )
        return float((row or {}).get("yes_ask") or 0) or None
    except Exception:
        return None


# ── Exit logic ────────────────────────────────────────────────────────────────

async def _check_and_exit(
    slot: Dict,
    kalshi,
    db,
    discord,
    reason_override: str = "",
) -> bool:
    """
    Evaluate one live slot. Return True if it was exited (slot should be freed).
    Checks:
      a) Market close_time has passed (resolved)
      b) Stop-loss: price dropped STOP_LOSS_PCT from entry (YES) or rose that much (NO)
    """
    ticker   = slot["ticker"]
    platform = slot.get("platform", "kalshi")
    side     = slot.get("side", "yes")
    entry    = float(slot.get("avg_price") or slot.get("entry_price") or 0)

    # a) Close time passed?
    close_time = slot.get("close_time") or ""
    if close_time:
        hl = _hours_left(close_time)
        if hl is not None and hl <= 0:
            logger.info("LIVE EXIT %s — market resolved (close_time passed)", ticker)
            await _close_position(db, ticker, reason_override or "market_resolved")
            await _send_exit_alert(discord, slot, reason="Market resolved ✅", current_price=None)
            return True

    if reason_override:
        await _close_position(db, ticker, reason_override)
        return True

    # b) Stop-loss check
    if platform == "kalshi":
        current = await _fetch_kalshi_price(ticker, kalshi)
    else:
        current = await _fetch_poly_price(ticker, db)

    if current is None or entry <= 0:
        return False

    if side == "yes":
        loss_pct = (entry - current) / entry * 100
    else:
        loss_pct = (current - entry) / entry * 100  # NO: we lose if YES price goes up

    if loss_pct >= STOP_LOSS_PCT:
        logger.warning(
            "LIVE STOP-LOSS %s | entry=%.0f¢ current=%.0f¢ loss=%.1f%% ≥ %.0f%%",
            ticker, entry, current, loss_pct, STOP_LOSS_PCT,
        )
        await _close_position(db, ticker, f"stop_loss:{loss_pct:.1f}%")
        await _send_exit_alert(
            discord, slot,
            reason=f"Stop-loss triggered — {loss_pct:.1f}% loss",
            current_price=current,
        )
        return True

    return False


# ── Fill slots ────────────────────────────────────────────────────────────────

async def _fill_slots(
    n_needed: int,
    kalshi,
    poly_client,
    db,
    discord,
    settings,
    kalshi_trader,
    poly_trader,
    scaler,
    risk,
) -> int:
    """Scan live markets (ALL categories), pick top n_needed by AI confidence, enter them."""
    if n_needed <= 0:
        return 0

    from src.strategy.opportunity import OpportunityHunter

    # Fetch live markets from BOTH platforms — all categories, any close time ≤ LIVE_WINDOW_HOURS
    live_k, live_p = [], []
    try:
        live_k = await kalshi.get_live_markets(max_hours=LIVE_WINDOW_HOURS, max_markets=60)
    except Exception as e:
        logger.debug("Kalshi live fetch: %s", e)
    try:
        live_p = await poly_client.get_live_markets(max_hours=LIVE_WINDOW_HOURS, max_markets=60)
    except Exception as e:
        logger.debug("Polymarket live fetch: %s", e)

    # Exclude already-open tickers
    open_tickers = set(_live_slots.keys())
    try:
        db_open = await db.fetchall("SELECT ticker FROM positions WHERE status='open'")
        open_tickers |= {r["ticker"] for r in (db_open or [])}
    except Exception:
        pass

    live_k = [m for m in live_k if m.get("ticker") not in open_tickers and 2 < (m.get("yes_ask") or 0) < 98]
    live_p = [m for m in live_p if m.get("ticker") not in open_tickers and m.get("yes_ask", 0) > 1]
    all_live = live_k + live_p

    if not all_live:
        logger.info("No fresh live markets available for slot fill")
        return 0

    logger.info(
        "Filling %d live slot(s) — scanning %d Kalshi + %d Polymarket live markets",
        n_needed, len(live_k), len(live_p),
    )

    hunter   = OpportunityHunter(db=db)
    top_live = await hunter.find_top_live(
        live_markets    = all_live,
        arb_signals     = [],
        min_confidence  = settings.trading.min_ai_confidence,
        top_n           = n_needed,
        ai_eval_n       = min(AI_EVAL_N, len(all_live)),
    )

    if not top_live:
        logger.info("No live market cleared confidence gate — staying under-filled")
        return 0

    portfolio_val = settings.trading.portfolio_value
    min_size      = settings.trading.min_trade_size_dollars
    max_size      = settings.trading.max_trade_size_dollars

    alert_trades = []
    entered = 0

    for r in top_live:
        m          = r["market"]
        decision   = r["decision"]
        side       = r["side"]
        price      = r["price_cents"]
        platform   = r["platform"]
        ticker     = m.get("ticker", "")
        confidence = float(decision.get("confidence", 0))
        net_ev     = decision.get("net_ev") or 0.0

        # Sizing
        mult = (1.5 if confidence >= 90 else
                1.0 if confidence >= 80 else
                0.5 if confidence >= 70 else 0.25)
        size = round(max(min_size, min(scaler.current_size * mult, max_size)), 2)

        # Profit gate
        contracts_est  = size / (price / 100) if price > 0 else 0
        exp_profit     = contracts_est * (net_ev / 100)
        roi_pct        = (exp_profit / size * 100) if size else 0
        if exp_profit < MIN_ABS_USD or roi_pct < MIN_ROI_PCT:
            logger.info(
                "LIVE FILL SKIP %s — profit gate: $%.2f (%.1f%% ROI)", ticker, exp_profit, roi_pct
            )
            continue

        # Risk gate
        allowed, rr = risk.check_trade(
            ticker, scaler.current_size,
            current_positions=[], portfolio_value=portfolio_val,
        )
        if not allowed:
            logger.info("LIVE FILL SKIP %s — risk: %s", ticker, rr)
            continue

        active_trader = kalshi_trader if platform == "kalshi" else poly_trader
        poly_kwargs   = ({"poly_token_id": m.get("_yes_token") if side == "yes" else m.get("_no_token")}
                         if platform == "polymarket" else {})

        rec = await active_trader.execute(
            ticker=ticker,
            action=decision["action"],
            side=side,
            price_cents=price,
            ai_confidence=confidence,
            ai_reasoning=decision["reasoning"],
            signal_source="live_scan",
            net_ev=net_ev,
            market_title=m.get("title", ""),
            **poly_kwargs,
        )
        if rec:
            _live_slots[ticker] = {
                "ticker":        ticker,
                "side":          side,
                "entry_price":   price,
                "platform":      platform,
                "title":         m.get("title", ""),
                "contracts":     rec.get("contracts", 0),
                "close_time":    m.get("close_time", ""),
                "hours_to_close": m.get("hours_to_close", 0),
                "confidence":    confidence,
                "net_ev":        net_ev,
                "reasoning":     decision.get("reasoning", ""),
                "size_usd":      size,
            }
            alert_trades.append(_live_slots[ticker])
            entered += 1
            logger.info(
                "LIVE SLOT FILLED [%s] %s BUY %s @ %.0f¢ conf=%d%% EV=%.1f¢",
                platform.upper(), ticker, side.upper(), price, confidence, net_ev,
            )

    if alert_trades:
        await discord.live_trades_alert(alert_trades, mode="PAPER" if not settings.trading.live_trading_enabled else "LIVE")

    return entered


# ── Exit alert ────────────────────────────────────────────────────────────────

async def _send_exit_alert(discord, slot: Dict, reason: str, current_price: Optional[float]) -> None:
    try:
        entry   = float(slot.get("entry_price") or slot.get("avg_price") or 0)
        ticker  = slot.get("ticker", "")
        title   = slot.get("title") or ticker
        side    = (slot.get("side") or "yes").upper()
        plat    = slot.get("platform", "kalshi")
        plat_icon = "🟦" if plat == "kalshi" else "🟣"
        pnl_str = ""
        if current_price and entry:
            contracts = int(slot.get("contracts") or 1)
            raw_pnl   = (current_price - entry) / 100 * contracts if side == "YES" else (entry - current_price) / 100 * contracts
            pnl_str   = f" | PnL ≈ ${raw_pnl:+.2f}"

        payload = {
            "embeds": [{
                "title": f"🔴 LIVE EXIT — {reason}",
                "description": f"{plat_icon} **{title[:80]}**",
                "color": 0xFF4444,
                "fields": [
                    {"name": "Ticker",   "value": ticker[:30],                                "inline": True},
                    {"name": "Your Bet", "value": f"BUY {side}",                              "inline": True},
                    {"name": "Entry",    "value": f"{entry:.0f}¢",                            "inline": True},
                    {"name": "Exit",     "value": f"{current_price:.0f}¢{pnl_str}" if current_price else "—", "inline": True},
                    {"name": "Reason",   "value": reason,                                     "inline": False},
                ],
                "timestamp": _now_utc().isoformat(),
                "footer": {"text": "Scanning for replacement…"},
            }]
        }
        await discord._post(payload)
    except Exception as e:
        logger.debug("Exit alert error: %s", e)


# ── Main manager loop ─────────────────────────────────────────────────────────

async def run_live_manager_cycle(db, discord, settings, kalshi_trader, poly_trader, scaler, risk) -> None:
    """One full cycle of the live market manager."""
    from src.clients.kalshi_client import KalshiClient
    from src.clients.polymarket_client import PolymarketTradingClient

    kalshi      = KalshiClient()
    poly_client = PolymarketTradingClient()

    try:
        # 1. Sync in-memory slots with DB (handles restarts / cross-cycle state)
        db_positions = await _load_live_positions(db)
        db_tickers   = {p["ticker"] for p in db_positions}

        # Remove slots that are no longer open in DB
        for t in list(_live_slots.keys()):
            if t not in db_tickers:
                logger.info("LIVE SLOT FREED (closed externally): %s", t)
                _live_slots.pop(t, None)

        # Add DB positions not yet in memory (e.g., after restart)
        for p in db_positions:
            t = p["ticker"]
            if t not in _live_slots:
                # Look up close_time from markets table
                mkt = await db.fetchone("SELECT close_time FROM markets WHERE ticker=?", (t,))
                _live_slots[t] = {
                    "ticker":      t,
                    "side":        p.get("side", "yes"),
                    "entry_price": p.get("avg_price", 0),
                    "platform":    p.get("platform", "kalshi"),
                    "title":       p.get("title", ""),
                    "contracts":   p.get("contracts", 0),
                    "close_time":  (mkt or {}).get("close_time", "") if mkt else "",
                    "confidence":  0,
                }

        logger.info("Live slots active: %d/%d", len(_live_slots), MAX_LIVE_POSITIONS)

        # 2. Check each slot — exit if stop-loss or resolved
        to_remove = []
        for ticker, slot in list(_live_slots.items()):
            exited = await _check_and_exit(slot, kalshi, db, discord)
            if exited:
                to_remove.append(ticker)

        for t in to_remove:
            _live_slots.pop(t, None)

        # 3. Fill empty slots
        n_empty = MAX_LIVE_POSITIONS - len(_live_slots)
        if n_empty > 0:
            logger.info("Filling %d empty live slot(s)…", n_empty)
            await _fill_slots(
                n_needed      = n_empty,
                kalshi        = kalshi,
                poly_client   = poly_client,
                db            = db,
                discord       = discord,
                settings      = settings,
                kalshi_trader = kalshi_trader,
                poly_trader   = poly_trader,
                scaler        = scaler,
                risk          = risk,
            )
        else:
            logger.info("All %d live slots occupied — next check in %ds", MAX_LIVE_POSITIONS, SCAN_INTERVAL)

    except Exception as e:
        logger.error("Live manager cycle error: %s", e, exc_info=True)
    finally:
        await kalshi.close()
        try:
            await poly_client._client().aclose()
        except Exception:
            pass
