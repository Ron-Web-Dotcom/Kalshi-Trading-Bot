"""
getlive_eventstoday.py — fetch live markets direct from Kalshi + Polymarket APIs
and display them in two tables with bot confidence, reasoning, and BID/WATCH/SKIP.

Falls back to DB cache if an API is unavailable (bot stopped, no credentials, etc.)

Usage (on VPS):
  cd /root/trading-bot
  python3 getlive_eventstoday.py
  python3 getlive_eventstoday.py --all           # include tomorrow's markets too
  python3 getlive_eventstoday.py --platform kalshi
  python3 getlive_eventstoday.py --platform polymarket
  python3 getlive_eventstoday.py --all-markets   # show junk/skipped rows too
  python3 getlive_eventstoday.py --min-conf 80   # change confidence threshold
  python3 getlive_eventstoday.py --db-only       # skip API, use DB cache only
"""

import argparse
import asyncio
import sqlite3
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# ── Column widths ─────────────────────────────────────────────────────────────
_TITLE_W  = 50
_CLOSE_W  = 30
_PRICE_W  = 6
_VOL_W    = 7
_CONF_W   = 8
_BID_W    = 11
_REASON_W = 28
_NOTES_W  = 40


# ─────────────────────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_et() -> datetime:
    return datetime.now(_ET)


def _parse_close(ct: str):
    if not ct:
        return None
    try:
        dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_ET)
    except Exception:
        return None


def _hours_left(ct_str: str) -> float:
    dt = _parse_close(ct_str)
    if dt is None:
        return float("inf")
    return (dt - _now_et()).total_seconds() / 3600


def _closes_today(ct_str: str) -> bool:
    dt = _parse_close(ct_str)
    return dt is not None and dt.date() == _now_et().date()


def _closes_tomorrow(ct_str: str) -> bool:
    dt = _parse_close(ct_str)
    return dt is not None and dt.date() == (_now_et() + timedelta(days=1)).date()


def _fmt_close(ct_str: str) -> str:
    dt = _parse_close(ct_str)
    if dt is None:
        return "unknown"
    hl = _hours_left(ct_str)
    if hl < 0:
        urgency = "CLOSED"
    elif hl < 1:
        urgency = f"{hl*60:.0f}m left"
    else:
        urgency = f"{hl:.1f}h left"
    return f"{dt.strftime('%m/%d %I:%M %p')} ET  ({urgency})"


# ─────────────────────────────────────────────────────────────────────────────
# Gate check — mirrors bot's pre-AI quality gates
# ─────────────────────────────────────────────────────────────────────────────

def _gate_check(row: dict) -> tuple:
    """Returns (gate_result, reason)  gate_result: PASS | SKIP | CLOSED"""
    from src.utils.junk_filter import is_junk

    title   = row.get("title") or ""
    yes_ask = float(row.get("yes_ask") or 0)
    volume  = float(row.get("volume") or 0)
    ct      = row.get("close_time") or ""
    hl      = _hours_left(ct)

    if hl < 0:
        return "CLOSED", "already closed"
    if 0 <= hl < 0.5:
        return "SKIP", "resolving now (<30m)"
    if is_junk(title):
        return "SKIP", "junk filter"
    if volume > 0 and volume < 50:
        return "SKIP", f"vol={volume:.0f} < 50"
    if yes_ask > 0 and yes_ask < 15:
        return "SKIP", f"long-shot {yes_ask:.0f}¢"
    if yes_ask > 0 and yes_ask > 95:
        return "SKIP", f"near-certain {yes_ask:.0f}¢"
    return "PASS", ""


# ─────────────────────────────────────────────────────────────────────────────
# BID label
# ─────────────────────────────────────────────────────────────────────────────

def _bid_label(gate: str, bot_action: str, bot_conf: float, min_conf: float, hl: float) -> tuple:
    if gate == "CLOSED":
        return "🔒 CLOSED", "already closed"
    if gate == "SKIP":
        return "⛔ SKIP", ""

    if bot_action == "BUY" and bot_conf >= min_conf:
        return "✅ BID YES", f"conf={bot_conf:.0f}%"
    if bot_action == "BUY" and bot_conf > 0:
        return "👀 WATCH", f"BUY conf={bot_conf:.0f}% < {min_conf:.0f}%"
    if bot_action == "HOLD" and bot_conf > 0:
        return "👀 WATCH", f"HOLD conf={bot_conf:.0f}%"

    # Not evaluated — show what threshold is needed
    needed = 70 if hl <= 6 else (75 if hl <= 24 else 88)
    return "👀 WATCH", f"not evaluated (need {needed:.0f}%)"


# ─────────────────────────────────────────────────────────────────────────────
# Table helpers
# ─────────────────────────────────────────────────────────────────────────────

def _trunc(s: str, w: int) -> str:
    s = (s or "").strip()
    return (s[: w - 1] + "…") if len(s) > w else s.ljust(w)


def _div(widths: list) -> str:
    return "+" + "+".join("-" * (w + 2) for w in widths) + "+"


def _hrow(cols: list, widths: list) -> str:
    return "|" + "|".join(f" {str(c).ljust(w)} " for c, w in zip(cols, widths)) + "|"


def _drow(vals: list, widths: list) -> str:
    return "|" + "|".join(f" {str(v).ljust(w)} " for v, w in zip(vals, widths)) + "|"


# ─────────────────────────────────────────────────────────────────────────────
# Print one platform table
# ─────────────────────────────────────────────────────────────────────────────

def _print_table(platform: str, rows: list, ai_map: dict, min_conf: float, show_skip: bool, source: str) -> None:
    icon = "🟦 KALSHI" if platform == "kalshi" else "🟣 POLYMARKET"
    print(f"\n{'═'*130}")
    print(f"  {icon}  [{source}]  —  {_now_et().strftime('%A %b %d %Y %I:%M %p ET')}")
    print(f"{'═'*130}")

    if not rows:
        print("  No markets found for today.\n")
        return

    cols = ["#", "TITLE", "CLOSES (ET)", "YES", "NO", "VOL",
            "BOT CONF", "BID?", "GATE / REASON", "BOT REASONING (latest)"]
    widths = [3, _TITLE_W, _CLOSE_W, _PRICE_W, _PRICE_W, _VOL_W,
              _CONF_W, _BID_W, _REASON_W, _NOTES_W]
    div = _div(widths)

    print(div)
    print(_hrow(cols, widths))
    print(div)

    bid_count = watch_count = skip_count = shown = 0

    for i, row in enumerate(rows, 1):
        gate, gate_reason = _gate_check(row)

        if gate == "SKIP" and not show_skip:
            skip_count += 1
            continue

        ct      = row.get("close_time") or ""
        ticker  = row.get("ticker") or ""
        yes_ask = float(row.get("yes_ask") or 0)
        no_ask  = float(row.get("no_ask")  or 0)
        volume  = float(row.get("volume")  or 0)
        hl      = _hours_left(ct)

        ai       = ai_map.get(ticker) or {}
        bot_act  = (ai.get("action") or "").upper()
        bot_conf = float(ai.get("confidence") or 0)
        bot_rsn  = (ai.get("reasoning") or "").strip()

        label, bid_reason = _bid_label(gate, bot_act, bot_conf, min_conf, hl)

        if gate == "SKIP":
            skip_count += 1
        elif "BID YES" in label:
            bid_count += 1
        elif "WATCH" in label:
            watch_count += 1

        shown += 1

        # Urgency prefix
        if gate not in ("CLOSED", "SKIP") and 0 < hl <= 1:
            close_str = f"⚡ {_fmt_close(ct)}"
        elif gate not in ("CLOSED", "SKIP") and 0 < hl <= 3:
            close_str = f"🔴 {_fmt_close(ct)}"
        else:
            close_str = f"   {_fmt_close(ct)}"

        reason_col = gate_reason if gate == "SKIP" else bid_reason
        conf_str   = f"{bot_conf:.0f}%" if bot_conf > 0 else "—"

        vals = [
            str(i),
            _trunc(row.get("title") or ticker, _TITLE_W),
            _trunc(close_str, _CLOSE_W),
            f"{yes_ask:.0f}¢" if yes_ask else "—",
            f"{no_ask:.0f}¢"  if no_ask  else "—",
            f"{volume:.0f}"   if volume   else "—",
            conf_str.ljust(_CONF_W),
            _trunc(label, _BID_W),
            _trunc(reason_col, _REASON_W),
            _trunc(bot_rsn, _NOTES_W),
        ]
        print(_drow(vals, widths))

    print(div)
    print(
        f"  Shown: {shown}  |  ✅ {bid_count} BID  |  👀 {watch_count} WATCH  |  ⛔ {skip_count} SKIP"
        + ("  (--all-markets to show skipped)" if skip_count and not show_skip else "")
    )
    print()


# ─────────────────────────────────────────────────────────────────────────────
# DB: load AI decisions + fallback markets
# ─────────────────────────────────────────────────────────────────────────────

def _load_db(db_path: str) -> tuple:
    """Returns (db_markets_by_platform, ai_map)"""
    if not os.path.exists(db_path):
        return {}, {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # All open markets from cache
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, title, close_time, yes_ask, no_ask, volume, "
        "       status, platform, last_price "
        "FROM markets "
        "WHERE (status='open' OR status='' OR status IS NULL) "
        "ORDER BY close_time ASC"
    )
    db_markets: dict = {"kalshi": [], "polymarket": []}
    for r in cur.fetchall():
        row = dict(r)
        plat = (row.get("platform") or "kalshi").lower()
        if plat in db_markets:
            db_markets[plat].append(row)

    # Today's AI decisions — most recent per ticker
    today_prefix = _now_et().date().isoformat()
    ai_map: dict = {}
    for table, action_col, conf_col, rsn_col, ts_col in [
        ("ai_decisions",  "action",       "confidence",    "reasoning",    "decided_at"),
        ("paper_signals", "action",       "ai_confidence", "ai_reasoning", "created_at"),
    ]:
        try:
            cur.execute(
                f"SELECT ticker, {action_col} AS action, {conf_col} AS confidence, "
                f"       {rsn_col} AS reasoning, {ts_col} AS decided_at "
                f"FROM {table} "
                f"WHERE {ts_col} >= ? ORDER BY {ts_col} DESC",
                (today_prefix + "T00:00:00",)
            )
            for row in cur.fetchall():
                r = dict(row)
                t = r["ticker"]
                if t not in ai_map:
                    ai_map[t] = r
        except Exception:
            pass

    conn.close()
    return db_markets, ai_map


# ─────────────────────────────────────────────────────────────────────────────
# Live API fetchers
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_kalshi_live(include_tomorrow: bool) -> tuple:
    """Fetch today's Kalshi markets live from API. Returns (markets, source_label)."""
    try:
        from src.clients.kalshi_client import KalshiClient
        client = KalshiClient()

        all_markets: list = []
        cursor = ""
        pages = 0
        while pages < 10:
            data   = await client.get_markets(limit=200, cursor=cursor, status="open")
            mkts   = data.get("markets") or []
            cursor = data.get("cursor") or ""
            all_markets.extend(mkts)
            pages += 1
            if not cursor or not mkts:
                break

        # Normalise fields
        today   = _now_et().date()
        result  = []
        for m in all_markets:
            ct = m.get("close_time") or m.get("expiration_time") or ""
            dt = _parse_close(ct)
            if dt is None:
                continue
            if not include_tomorrow and dt.date() != today:
                continue
            if include_tomorrow and dt.date() not in (today, (datetime.now(_ET) + timedelta(days=1)).date()):
                continue
            result.append({
                "ticker":     m.get("ticker", ""),
                "title":      m.get("title", ""),
                "close_time": ct,
                "yes_ask":    float(m.get("yes_ask") or 0),
                "no_ask":     float(m.get("no_ask")  or 0),
                "volume":     float(m.get("volume")  or 0),
                "status":     m.get("status", "open"),
                "platform":   "kalshi",
            })

        result.sort(key=lambda r: r.get("close_time") or "")
        return result, f"LIVE API — {len(result)} markets"
    except Exception as e:
        return [], f"API error: {e}"


async def _fetch_poly_live(include_tomorrow: bool) -> tuple:
    """Fetch today's Polymarket markets live from Gamma API. Returns (markets, source_label)."""
    try:
        import httpx
        _now_et_dt  = _now_et()
        _eod_et     = _now_et_dt.replace(hour=23, minute=59, second=59)
        if include_tomorrow:
            _eod_et = _eod_et + timedelta(days=1)
        _now_utc = _now_et_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _eod_utc = _eod_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        GAMMA = "https://gamma-api.polymarket.com"
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{GAMMA}/markets", params={
                "active":       "true",
                "closed":       "false",
                "limit":        500,
                "end_date_min": _now_utc,
                "end_date_max": _eod_utc,
            })

        if r.status_code != 200:
            return [], f"Gamma HTTP {r.status_code}"

        raw   = r.json()
        items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])

        result = []
        for m in items:
            ct = (m.get("endDate") or m.get("end_date") or
                  m.get("close_time") or m.get("closeTime") or "")
            title = (m.get("question") or m.get("title") or
                     m.get("market_slug") or "").strip()
            if not title:
                continue

            # Parse outcome prices (tokens list or direct fields)
            yes_ask = 0.0
            no_ask  = 0.0
            tokens  = m.get("tokens") or m.get("outcomes") or []
            for tok in tokens:
                name  = (tok.get("outcome") or tok.get("name") or "").lower()
                price = float(tok.get("price") or 0) * 100
                if "yes" in name:
                    yes_ask = price
                elif "no" in name:
                    no_ask  = price
            if yes_ask == 0:
                yes_ask = float(m.get("lastTradePrice") or m.get("last_trade_price") or 0) * 100

            result.append({
                "ticker":     m.get("conditionId") or m.get("id") or "",
                "title":      title,
                "close_time": ct,
                "yes_ask":    yes_ask,
                "no_ask":     no_ask,
                "volume":     float(m.get("volume") or m.get("volumeNum") or 0),
                "status":     "open",
                "platform":   "polymarket",
            })

        result.sort(key=lambda r: r.get("close_time") or "")
        return result, f"LIVE API — {len(result)} markets"
    except Exception as e:
        return [], f"API error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def _run(args):
    # ── Resolve DB path ───────────────────────────────────────────────────────
    db_path = args.db
    if db_path is None:
        for c in [
            os.path.join(os.path.dirname(__file__), "trading_system.db"),
            os.path.expanduser("~/trading-bot/trading_system.db"),
            "/root/trading-bot/trading_system.db",
            "trading_system.db",
        ]:
            if os.path.exists(c):
                db_path = c
                break

    print(f"\n  DB : {db_path or 'not found'}")
    print(f"  Now: {_now_et().strftime('%A %B %d %Y %I:%M:%S %p ET')}")

    # ── Load AI decisions + DB fallback markets ────────────────────────────────
    db_markets, ai_map = _load_db(db_path) if db_path else ({}, {})
    evaluated = len(ai_map)
    print(f"  Bot evaluations in DB today: {evaluated} tickers")

    platforms = ["kalshi", "polymarket"]
    if args.platform:
        platforms = [args.platform]

    # ── Fetch markets ─────────────────────────────────────────────────────────
    for plat in platforms:
        markets = []
        source  = "DB cache"

        if not args.db_only:
            print(f"\n  Fetching {plat.upper()} from live API…", end="", flush=True)
            if plat == "kalshi":
                markets, source = await _fetch_kalshi_live(args.all)
            else:
                markets, poly_source = await _fetch_poly_live(args.all)
                markets, source = markets, poly_source
            print(f" {source}")

        # Fallback to DB cache if API returned nothing
        if not markets:
            cache = db_markets.get(plat, [])
            if cache:
                # Filter to today/tomorrow from DB cache
                filtered = []
                for r in cache:
                    ct = r.get("close_time") or ""
                    if args.all:
                        if _closes_today(ct) or _closes_tomorrow(ct):
                            filtered.append(r)
                    else:
                        if _closes_today(ct):
                            filtered.append(r)
                markets = filtered
                source  = f"DB cache ({len(markets)} markets)"
                if not markets and cache:
                    # Cache has markets but wrong dates — show most recent
                    markets = sorted(cache, key=lambda r: r.get("close_time") or "")[:50]
                    source  = f"DB cache — stale (bot stopped, showing {len(markets)} cached)"
            if not markets:
                source = "no data — bot stopped + no DB cache"

        _print_table(plat, markets, ai_map, args.min_conf, args.all_markets, source)

    print("  💡 --all-markets     → show junk/skip rows")
    print("  💡 --all             → include tomorrow's markets")
    print("  💡 --platform kalshi | polymarket")
    print("  💡 --db-only         → skip live API, use DB cache only")
    print("  💡 --min-conf 80     → change BID threshold\n")


def main():
    parser = argparse.ArgumentParser(
        description="Show today's live events with bot confidence + BID/WATCH/SKIP"
    )
    parser.add_argument("--platform",    choices=["kalshi", "polymarket"])
    parser.add_argument("--all",         action="store_true", help="Include tomorrow too")
    parser.add_argument("--all-markets", action="store_true", help="Show junk/skipped rows")
    parser.add_argument("--min-conf",    type=float, default=75.0)
    parser.add_argument("--db",          default=None, help="Path to trading_system.db")
    parser.add_argument("--db-only",     action="store_true", help="Skip live API, use DB only")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
