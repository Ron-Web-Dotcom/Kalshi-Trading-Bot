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

from src.utils.junk_filter import is_junk

logger = logging.getLogger("trading.live_manager")

MAX_LIVE_POSITIONS   = 10    # up to 10 in-play trades — broad coverage across all live categories
SCAN_INTERVAL        = 300   # seconds between manager cycles (5 minutes)
LIVE_WINDOW_HOURS    = 24.0  # Kalshi: live events happening today only
POLY_LIVE_WINDOW_HOURS = 24.0  # Polymarket: live events happening today only
STOP_LOSS_PCT        = 40.0  # exit if current_price dropped this % from entry (YES side)
AI_EVAL_N            = 12    # how many pre-scored markets to send to AI per fill cycle
MIN_ROI_PCT          = 1.0   # minimum ROI% for live trades (relaxed — live markets resolve fast)
MIN_ABS_USD          = 0.25  # minimum absolute expected profit per live trade

# in-memory slot registry — ticker → slot dict
_live_slots: Dict[str, Dict] = {}
# last price reported per ticker — used to suppress no-change spam
_last_reported_price: Dict[str, float] = {}
_last_price_alert_time: Optional[datetime] = None
PRICE_CHANGE_THRESHOLD = 5.0   # percent — significant move threshold
PRICE_ALERT_INTERVAL   = 600   # seconds — check at most every 10 min


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
              AND EXISTS (
                  SELECT 1 FROM trade_logs tl
                   WHERE tl.ticker = p.ticker AND tl.signal_source = 'live_scan'
              )
        """)
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.warning("_load_live_positions error: %s", e)
        return []


async def _close_position(db, ticker: str, reason: str, slot: Optional[Dict] = None, exit_price: Optional[float] = None) -> None:
    """Mark a position closed in the DB, writing final pnl if we have price data."""
    try:
        now = _now_utc().isoformat()
        if slot is not None and exit_price is not None:
            pnl, _, _ = _calc_pnl(slot, exit_price)
            await db.execute("""
                UPDATE positions
                   SET status='closed', closed_at=?, close_reason=?,
                       current_price=?, exit_price=?, pnl=?
                 WHERE ticker=? AND status='open'
            """, (now, reason[:200], exit_price, exit_price, pnl, ticker))
            # Stamp the matching trade_log so W/L stats are counted.
            # SQLite does not support ORDER BY/LIMIT in UPDATE — use a subquery instead.
            result_str = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAK_EVEN")
            platform = (slot or {}).get("platform") or "kalshi"
            await db.execute("""
                UPDATE trade_logs
                   SET pnl=?, result=?, resolved_at=?
                 WHERE id = (
                     SELECT id FROM trade_logs
                      WHERE ticker=? AND signal_source='live_scan'
                        AND (pnl IS NULL OR pnl=0)
                        AND (platform=? OR platform IS NULL)
                      ORDER BY executed_at DESC
                      LIMIT 1
                 )
            """, (pnl, result_str, now, ticker, platform))
        else:
            # Fallback: close without overwriting existing pnl
            await db.execute("""
                UPDATE positions
                   SET status='closed', closed_at=?, close_reason=?
                 WHERE ticker=? AND status='open'
            """, (now, reason[:200], ticker))
    except Exception as e:
        logger.warning("_close_position error %s: %s", ticker, e)


# ── Price fetching ────────────────────────────────────────────────────────────

async def _fetch_kalshi_market(ticker: str, kalshi) -> Optional[Dict]:
    try:
        data = await kalshi._request("GET", f"/markets/{ticker}")
        return data.get("market") or data
    except Exception:
        return None


async def _fetch_kalshi_price(ticker: str, kalshi, side: str = "yes") -> Optional[float]:
    """Return the current ask price for the given side (yes_ask or no_ask)."""
    m = await _fetch_kalshi_market(ticker, kalshi)
    if not m:
        return None
    bid_key = "yes_ask" if side == "yes" else "no_ask"
    price = m.get(bid_key) or 0
    if not price:
        price = m.get("last_price") or 0
        if price and side == "no":
            price = 100.0 - float(price)  # approximate NO from last_price
    return float(price) if price else None


async def _fetch_poly_price(ticker: str, db, side: str = "yes") -> Optional[float]:
    """Read last known price from DB markets table (updated by trade job)."""
    try:
        col = "yes_ask" if side == "yes" else "no_ask"
        row = await db.fetchone(
            f"SELECT yes_ask, no_ask FROM markets WHERE ticker=?", (ticker,)
        )
        if not row:
            return None
        price_raw = row.get(col)
        price = float(price_raw) if price_raw is not None else None
        if price is None:
            other_col = "yes_ask" if side == "no" else "no_ask"
            other_raw = row.get(other_col)
            other = float(other_raw) if other_raw is not None else None
            if other is not None:
                price = 100.0 - other
        return price
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

    # Fetch current price for the position's own side (YES or NO)
    if platform == "kalshi":
        current = await _fetch_kalshi_price(ticker, kalshi, side=side)
    else:
        current = await _fetch_poly_price(ticker, db, side=side)

    # Keep slot's current_price fresh for the live position update alert
    if current is not None:
        slot["current_price"] = current

    # a) Close time passed → market resolved, check win/loss from final price
    close_time = slot.get("close_time") or ""
    if close_time:
        hl = _hours_left(close_time)
        if hl is not None and hl <= 0:
            logger.info("LIVE EXIT %s — market resolved (close_time passed)", ticker)
            safe_price = current if current is not None else entry
            await _close_position(db, ticker, reason_override or "market_resolved", slot=slot, exit_price=safe_price)
            await _send_resolution_alert(discord, slot, final_price=current)
            return True

    if reason_override:
        safe_price = current if current is not None else entry
        await _close_position(db, ticker, reason_override, slot=slot, exit_price=safe_price)
        return True

    # b) Stop-loss check
    if current is None or entry <= 0:
        return False

    # current is the side's own price (NO ask for NO bets, YES ask for YES bets)
    # We lose when our side's price falls below entry
    loss_pct = (entry - current) / entry * 100 if entry else 0

    if loss_pct >= STOP_LOSS_PCT:
        logger.warning(
            "LIVE STOP-LOSS %s | entry=%.0f¢ current=%.0f¢ loss=%.1f%% ≥ %.0f%%",
            ticker, entry, current, loss_pct, STOP_LOSS_PCT,
        )
        await _close_position(db, ticker, f"stop_loss:{loss_pct:.1f}%", slot=slot, exit_price=current)
        await _send_stopout_alert(discord, slot, current_price=current, loss_pct=loss_pct)
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

    # Fetch live markets from BOTH platforms
    live_k, live_p = [], []

    # Kalshi: use confirmed live-now API (sports in progress, World Cup, debates, etc.)
    try:
        live_k = await kalshi.get_live_now_markets(max_markets=100)
        logger.info("Kalshi live now: %d confirmed in-play markets", len(live_k))
    except Exception as e:
        logger.warning("Kalshi live fetch FAILED: %s", e)

    # Polymarket: use actual in-play game API first, fall back to time-window
    try:
        live_p = await poly_client.get_live_now_markets(max_markets=100)
        if not live_p:
            logger.info("Polymarket live-now returned 0 — falling back to %dh window", POLY_LIVE_WINDOW_HOURS)
            live_p = await poly_client.get_live_markets(max_hours=POLY_LIVE_WINDOW_HOURS, max_markets=60)
        logger.info("Polymarket live: %d markets", len(live_p))
    except Exception as e:
        logger.warning("Polymarket live fetch FAILED: %s", e)
        try:
            live_p = await poly_client.get_live_markets(max_hours=POLY_LIVE_WINDOW_HOURS, max_markets=60)
        except Exception:
            pass

    # Exclude already-open tickers
    open_tickers = set(_live_slots.keys())
    try:
        db_open = await db.fetchall("SELECT ticker FROM positions WHERE status='open'")
        open_tickers |= {r["ticker"] for r in (db_open or [])}
    except Exception:
        pass

    # Live scan = TODAY only (midnight to midnight ET)
    try:
        from src.utils.eastern_time import now_et as _lm_now_et
        import zoneinfo as _lm_zi
        _et_now = _lm_now_et()
        _tonight_et = _et_now.replace(hour=23, minute=59, second=59, microsecond=0)
        _tonight_utc = _tonight_et.astimezone(timezone.utc)
    except Exception:
        _tonight_utc = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=0)

    def _closes_today(m: Dict) -> bool:
        ct = m.get("close_time") or ""
        if not ct:
            return False
        try:
            cd = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            if cd.tzinfo is None:
                cd = cd.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return now < cd <= _tonight_utc
        except Exception:
            return False

    def _kalshi_price(m: Dict) -> float:
        """Best available price for a Kalshi market — yes_ask with last_price fallback."""
        p = float(m.get("yes_ask") or 0)
        if not p:
            p = float(m.get("last_price") or 0)
        return p

    raw_k, raw_p = len(live_k), len(live_p)
    for m in live_k:
        if not (m.get("yes_ask") or 0) and (m.get("last_price") or 0):
            m["yes_ask"] = float(m["last_price"])
            m["no_ask"]  = round(100.0 - float(m["last_price"]), 1)
    live_k = [
        m for m in live_k
        if m.get("ticker") not in open_tickers
        and 8 < _kalshi_price(m) < 92               # min 8¢ — no near-resolved markets
        and _closes_today(m)                         # TODAY only (midnight to midnight ET)
        and not is_junk(m.get("title", ""))
    ]
    live_p = [
        m for m in live_p
        if m.get("ticker") not in open_tickers
        and m.get("yes_ask", 0) > 8                 # min 8¢ — no near-resolved markets
        and _closes_today(m)                         # TODAY only (midnight to midnight ET)
        and not is_junk(m.get("title", ""))
    ]

    if not live_k and raw_k:
        try:
            raw_sample = (await kalshi.get_all_markets(status="open", max_markets=1, sort_by_close=True) or [None])[0]
        except Exception:
            raw_sample = None
        logger.warning(
            "Kalshi: %d live markets fetched but all filtered out. Sample keys+prices: %s",
            raw_k,
            {k: raw_sample.get(k) for k in ["ticker","yes_ask","no_ask","last_price","yes_bid","no_bid","close_time"]} if raw_sample else "none",
        )
    if not live_p and raw_p:
        logger.warning("Polymarket: %d live markets fetched but all filtered out (0 price or already open)", raw_p)
    logger.info(
        "After price/dedup filter: %d/%d Kalshi + %d/%d Polymarket eligible",
        len(live_k), raw_k, len(live_p), raw_p,
    )
    all_live = live_k + live_p

    if not all_live:
        logger.info("No fresh live markets available for slot fill — all filtered out")
        return 0

    logger.info(
        "Filling %d live slot(s) — scanning %d Kalshi + %d Polymarket live markets",
        n_needed, len(live_k), len(live_p),
    )

    hunter   = OpportunityHunter(db=db)
    # Live markets resolve fast — use slightly lower confidence gate than regular trades
    live_min_conf = max(55.0, settings.trading.min_ai_confidence - 5.0)
    top_live = await hunter.find_top_live(
        live_markets    = all_live,
        arb_signals     = [],
        min_confidence  = live_min_conf,
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

        # Profit gate — use EV if available, else estimate from confidence edge
        contracts_est = size / (max(price, 1) / 100) if price > 0 else 0
        if net_ev > 0:
            exp_profit = contracts_est * (net_ev / 100)
        else:
            edge = max(0.0, confidence - 65) * 0.002
            exp_profit = size * edge if edge > 0 else MIN_ABS_USD
        roi_pct = (exp_profit / size * 100) if size else 0
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
        _mode = "PAPER" if not settings.trading.live_trading_enabled else "LIVE"
        # Mark as live so bot_alert renders the red LIVE IN-PLAY urgency tier
        for t in alert_trades:
            t["is_live"] = True
        await discord.bot_alert(alert_trades, mode=_mode)

    return entered


# ── Exit alerts ───────────────────────────────────────────────────────────────

def _calc_pnl(slot: Dict, exit_price: float) -> tuple:
    """Return (pnl_dollars, pnl_pct, capital_in) for a slot."""
    entry     = float(slot.get("entry_price") or slot.get("avg_price") or 0)
    contracts = int(slot.get("contracts") if slot.get("contracts") is not None else 1)
    side      = (slot.get("side") or "yes").lower()
    size_usd  = float(slot.get("size_usd") or (entry * contracts / 100))

    # exit_price is always the position's own side price — same formula for YES and NO
    pnl = (exit_price - entry) / 100 * contracts

    pnl_pct = (pnl / size_usd * 100) if size_usd else 0
    return round(pnl, 2), round(pnl_pct, 1), round(size_usd, 2)


async def _send_resolution_alert(discord, slot: Dict, final_price: Optional[float]) -> None:
    """Alert when a live market resolves naturally (we stayed in — opted in)."""
    try:
        from src.utils.eastern_time import format_et, et_label
        entry     = float(slot.get("entry_price") or slot.get("avg_price") or 0)
        ticker    = slot.get("ticker", "")
        title     = slot.get("title") or ticker
        side      = (slot.get("side") or "yes").upper()
        contracts = int(slot.get("contracts") if slot.get("contracts") is not None else 1)
        plat      = slot.get("platform", "kalshi")
        plat_icon = "🟦" if plat == "kalshi" else "🟣"
        conf      = float(slot.get("confidence") or 0)
        size_usd  = float(slot.get("size_usd") or 0)
        et_time   = format_et(fmt="%I:%M %p") + f" {et_label()}"

        # Determine win/loss from final price — final_price is the position's own side price
        # Both YES and NO win when their own side price rises to ~100¢
        if final_price is not None:
            won = final_price >= 85
            pnl, pnl_pct, _ = _calc_pnl(slot, final_price)
            exit_str = f"{final_price:.0f}¢"
        else:
            # Can't fetch final price — estimate from market side
            won = None
            pnl, pnl_pct = 0.0, 0.0
            exit_str = "resolving…"

        if won is True:
            result_emoji = "✅"
            result_label = "WON — We stayed in and it paid off!"
            color        = 0x00FF7F
            pnl_line     = f"**+${pnl:.2f}** (+{pnl_pct:.1f}%)"
        elif won is False:
            result_emoji = "❌"
            result_label = "LOST — Market resolved against us"
            color        = 0xFF4444
            pnl_line     = f"**${pnl:.2f}** ({pnl_pct:.1f}%)"
        else:
            result_emoji = "⏳"
            result_label = "Market closed — result pending settlement"
            color        = 0xAAAAAA
            pnl_line     = "Pending"

        max_payout = contracts * (100 - entry) / 100  # max profit = contracts × (1 - entry_cost)

        payload = {
            "embeds": [{
                "title": f"{result_emoji} LIVE RESULT — {result_label}",
                "description": f"{plat_icon} **{title[:90]}**",
                "color": color,
                "fields": [
                    {"name": "📌 Ticker",      "value": ticker[:30],        "inline": True},
                    {"name": "🎯 Our Bet",     "value": f"BUY {side}",      "inline": True},
                    {"name": "🏦 Platform",    "value": plat.capitalize(),   "inline": True},
                    {"name": "💵 Capital In",  "value": f"${size_usd:.2f}", "inline": True},
                    {"name": "📈 Entry Price", "value": f"{entry:.0f}¢",    "inline": True},
                    {"name": "🏁 Exit Price",  "value": exit_str,           "inline": True},
                    {"name": "💰 Result",      "value": pnl_line,           "inline": True},
                    {"name": "🎰 Max Payout",  "value": f"${max_payout:.2f}", "inline": True},
                    {"name": "🤖 AI Conf",     "value": f"{conf:.0f}%",     "inline": True},
                ],
                "timestamp": _now_utc().isoformat(),
                "footer": {"text": f"Opted in — held to resolution • {et_time} • Scanning for next opportunity…"},
            }]
        }
        await discord._post(payload)
        logger.info(
            "LIVE RESULT %s | %s | entry=%.0f¢ exit=%s pnl=%s",
            ticker, "WIN" if won else "LOSS" if won is False else "PENDING",
            entry, exit_str, pnl_line,
        )
    except Exception as e:
        logger.warning("Resolution alert error: %s", e)


async def _send_stopout_alert(discord, slot: Dict, current_price: float, loss_pct: float) -> None:
    """Alert when we exit early via stop-loss (opted out)."""
    try:
        from src.utils.eastern_time import format_et, et_label
        entry     = float(slot.get("entry_price") or slot.get("avg_price") or 0)
        ticker    = slot.get("ticker", "")
        title     = slot.get("title") or ticker
        side      = (slot.get("side") or "yes").upper()
        plat      = slot.get("platform", "kalshi")
        plat_icon = "🟦" if plat == "kalshi" else "🟣"
        conf      = float(slot.get("confidence") or 0)
        pnl, pnl_pct, size_usd = _calc_pnl(slot, current_price)
        et_time   = format_et(fmt="%I:%M %p") + f" {et_label()}"

        payload = {
            "embeds": [{
                "title": f"🛑 LIVE OPT-OUT — Stop-Loss Triggered",
                "description": (
                    f"{plat_icon} **{title[:90]}**\n"
                    f"Price moved **{loss_pct:.1f}%** against us — cutting losses before it gets worse."
                ),
                "color": 0xFF8C00,
                "fields": [
                    {"name": "📌 Ticker",       "value": ticker[:30],           "inline": True},
                    {"name": "🎯 Our Bet",      "value": f"BUY {side}",         "inline": True},
                    {"name": "🏦 Platform",     "value": plat.capitalize(),      "inline": True},
                    {"name": "💵 Capital In",   "value": f"${size_usd:.2f}",    "inline": True},
                    {"name": "📈 Entry Price",  "value": f"{entry:.0f}¢",       "inline": True},
                    {"name": "📉 Exit Price",   "value": f"{current_price:.0f}¢", "inline": True},
                    {"name": "💸 Loss",         "value": f"**${pnl:.2f}** ({pnl_pct:.1f}%)", "inline": True},
                    {"name": "🛑 Stop Trigger", "value": f"{loss_pct:.1f}% drop (limit: {STOP_LOSS_PCT:.0f}%)", "inline": True},
                    {"name": "🤖 AI Conf Was",  "value": f"{conf:.0f}%",        "inline": True},
                ],
                "timestamp": _now_utc().isoformat(),
                "footer": {"text": f"Opted out — stop-loss exit • {et_time} • Scanning for replacement…"},
            }]
        }
        await discord._post(payload)
        logger.info(
            "LIVE STOP-OUT %s | entry=%.0f¢ exit=%.0f¢ loss=$%.2f (%.1f%%)",
            ticker, entry, current_price, pnl, pnl_pct,
        )
    except Exception as e:
        logger.warning("Stop-out alert error: %s", e)


async def _send_live_positions_update(discord, slots: Dict) -> None:
    """
    Send a compact live-positions update embed.
    Only fires when at least one slot has moved >= PRICE_CHANGE_THRESHOLD since
    the last report — prevents spam on quiet cycles.
    """
    if not slots or not discord:
        return False
    try:
        from src.utils.eastern_time import format_et, et_label
        changed = []
        for ticker, slot in slots.items():
            cur = float(slot.get("current_price") or slot.get("entry_price") or 0)
            last = _last_reported_price.get(ticker, cur)
            if last <= 0 or abs(cur - last) / last * 100 >= PRICE_CHANGE_THRESHOLD:
                changed.append((ticker, slot, cur, last))

        if not changed:
            return False  # nothing moved enough — skip this cycle

        lines = []
        total_pnl = 0.0
        for ticker, slot, cur, last in changed:
            entry     = float(slot.get("entry_price") or slot.get("avg_price") or cur)
            contracts = int(slot.get("contracts") if slot.get("contracts") is not None else 1)
            side      = (slot.get("side") or "yes").upper()
            plat      = slot.get("platform", "kalshi")
            plat_icon = "🟦" if plat == "kalshi" else "🟣"
            title     = (slot.get("title") or ticker)[:50]
            pct_from_entry = ((cur - entry) / entry * 100) if entry else 0
            pct_sign  = "+" if pct_from_entry >= 0 else ""
            pnl, _, _ = _calc_pnl(slot, cur)
            total_pnl += pnl
            pnl_sign  = "+" if pnl >= 0 else ""
            move_icon = "📈" if cur >= last else "📉"
            move_pct  = (cur - last) / last * 100 if last else 0
            lines.append(
                f"{move_icon} {plat_icon} **{title}** | {side}\n"
                f"   {last:.0f}¢ → **{cur:.0f}¢** ({move_pct:+.1f}% this update) | "
                f"Entry {entry:.0f}¢ ({pct_sign}{pct_from_entry:.1f}%) | PnL **${pnl_sign}{pnl:.2f}**"
            )
            _last_reported_price[ticker] = cur

        # Also show unchanged slots quietly
        unchanged = [(t, s) for t, s in slots.items() if t not in {c[0] for c in changed}]
        for ticker, slot in unchanged:
            cur   = float(slot.get("current_price") or slot.get("entry_price") or 0)
            entry = float(slot.get("entry_price") or slot.get("avg_price") or cur)
            side  = (slot.get("side") or "yes").upper()
            plat_icon = "🟦" if slot.get("platform") == "kalshi" else "🟣"
            title = (slot.get("title") or ticker)[:50]
            pct   = ((cur - entry) / entry * 100) if entry else 0
            pnl, _, _ = _calc_pnl(slot, cur)
            total_pnl += pnl
            pnl_sign = "+" if pnl >= 0 else ""
            lines.append(
                f"➡️ {plat_icon} **{title}** | {side}\n"
                f"   {cur:.0f}¢ (no change) | Entry {entry:.0f}¢ ({pct:+.1f}%) | PnL **${pnl_sign}{pnl:.2f}**"
            )

        total_sign = "+" if total_pnl >= 0 else ""
        et_time = format_et(fmt="%I:%M %p") + f" {et_label()}"
        payload = {
            "embeds": [{
                "title": f"⚡ Live Position Update — {len(slots)} active",
                "description": (
                    "\n\n".join(lines)
                    + f"\n\n**Total Live PnL: ${total_sign}{total_pnl:.2f}**"
                ),
                "color": 0x00BFFF,
                "timestamp": _now_utc().isoformat(),
                "footer": {"text": f"Live monitor • {et_time} • Updates when price moves >{PRICE_CHANGE_THRESHOLD:.0f}%"},
            }]
        }
        await discord._post(payload)
        logger.info("Live position update sent (%d positions changed)", len(changed))
        return True
    except Exception as e:
        logger.warning("Live positions update alert error: %s", e)
        return False


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
                if is_junk(p.get("title") or ""):
                    logger.info("LIVE SLOT SKIPPED (junk title): %s", t)
                    continue
                # Look up close_time from markets table (works for Kalshi tickers)
                mkt = await db.fetchone(
                    "SELECT close_time FROM markets WHERE ticker=? OR ticker LIKE ?",
                    (t, f"%{t[:20]}%"),
                )
                close_time = (mkt or {}).get("close_time") or ""
                # Fallback: if we can't find close_time, try fetching live from Kalshi API
                if not close_time and p.get("platform", "kalshi") == "kalshi":
                    try:
                        raw = await kalshi._request("GET", f"/markets/{t}")
                        close_time = (raw.get("market") or raw).get("close_time") or ""
                    except Exception:
                        pass
                entry_p = float(p.get("avg_price") or 0)
                _live_slots[t] = {
                    "ticker":        t,
                    "side":          p.get("side", "yes"),
                    "entry_price":   entry_p,
                    "current_price": float(p.get("current_price") or entry_p),
                    "platform":      p.get("platform", "kalshi"),
                    "title":         p.get("title", ""),
                    "contracts":     p.get("contracts", 0),
                    "close_time":    close_time,
                    "confidence":    0,
                    "size_usd":      0,
                }
                # Seed last-reported price so restart doesn't fire a fake update
                if t not in _last_reported_price:
                    _last_reported_price[t] = float(p.get("current_price") or entry_p)

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

        # 4. Live position update — at most every 10 min, only if significant move
        if _live_slots:
            global _last_price_alert_time
            now_utc = _now_utc()
            elapsed = (now_utc - _last_price_alert_time).total_seconds() if _last_price_alert_time else PRICE_ALERT_INTERVAL + 1
            if elapsed >= PRICE_ALERT_INTERVAL:
                try:
                    sent = await _send_live_positions_update(discord, _live_slots)
                except Exception:
                    sent = False
                _last_price_alert_time = now_utc  # always update to avoid retry-every-5min loop

    except Exception as e:
        logger.error("Live manager cycle error: %s", e, exc_info=True)
    finally:
        await kalshi.close()
        try:
            await poly_client.close()
        except Exception:
            pass
