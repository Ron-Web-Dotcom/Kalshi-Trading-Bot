"""
getlive_eventstoday.py — fetch live markets direct from Kalshi + Polymarket APIs
and display them in tables with bot confidence, reasoning, and BID/WATCH/SKIP.

Falls back to DB cache if live API unavailable (bot stopped).

Usage (on VPS):
  cd /root/trading-bot
  python3 getlive_eventstoday.py
  python3 getlive_eventstoday.py --days 3        # look ahead N days (default 1 = today)
  python3 getlive_eventstoday.py --platform kalshi
  python3 getlive_eventstoday.py --platform polymarket
  python3 getlive_eventstoday.py --all-markets   # show junk/skipped rows too
  python3 getlive_eventstoday.py --min-conf 80   # change confidence threshold
  python3 getlive_eventstoday.py --db-only       # skip API, use DB cache only
"""

import argparse
import asyncio
import shutil
import sqlite3
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# ── Column widths — auto-scaled to terminal width at runtime ──────────────────
# Fixed columns: #(3) CLOSES(21) YES(5) NO(5) VOL(8) CONF(7) BID(9) REASON(20)
# = 78 fixed chars + 8 separators*3 + 2 outer pipes = 78 + 26 = 104 overhead
# Remaining space goes to TITLE.
_FIXED_OVERHEAD = 78 + 26   # fixed cols + all pipe+space padding

def _col_widths(term_w: int = 0) -> tuple:
    if term_w <= 0:
        term_w = shutil.get_terminal_size(fallback=(120, 40)).columns
    title_w  = max(28, min(60, term_w - _FIXED_OVERHEAD))
    reason_w = 20
    return (3, title_w, 21, 5, 5, 8, 7, 9, reason_w)

# ── Sub-market patterns to skip (keep only main outcomes) ─────────────────────
_SUBMARKET_SKIP = [
    # Financial options
    "target price:", "yes $", "no $", "or above,yes", "or above,no",
    "$0.0", "above,yes", "above,no",
    # Exact score / multi-outcome
    "exact score:", "exact score ",
    "any other score",
    # Halftime / period markets
    "leading at halftime", "at halftime", "halftime",
    "to win the second half", "second half draw", "second half",
    "to win the first half", "first half",
    # In-game events
    "first goal", "first blood", "first dragon", "first baron", "first tower",
    "to score first", "red card", "yellow card",
    "corner kicks", "total goals",
    "both teams to score", "both teams",
    # Over/Under / spread / handicap
    "over/under", "o/u 0.", "o/u 1.", "o/u 2.", "o/u 3.", "o/u 4.", "o/u 5.",
    "spread:", "handicap", "asian handicap",
    # Polymarket soccer sub-markets (1st half result, draw, team score lines)
    ": draw", ": 1st >", ": both", ": fk m", ": univ",
    "to win on penalties", "extra time",
    # Long-shot tournament winners
    "to win the 2026", "win the championship",
]


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



def _fmt_close(ct_str: str) -> str:
    dt = _parse_close(ct_str)
    if dt is None:
        return "unknown"
    hl = _hours_left(ct_str)
    if hl < 0:
        tag = "CLOSED"
    elif hl < 1:
        tag = f"{hl*60:.0f}m"
    else:
        tag = f"{hl:.1f}h"
    return f"{dt.strftime('%m/%d %I:%M%p')} ({tag})"


# ─────────────────────────────────────────────────────────────────────────────
# Gate check
# ─────────────────────────────────────────────────────────────────────────────

def _gate_check(row: dict) -> tuple:
    """Returns (gate_result, reason)   gate_result: PASS | SKIP | CLOSED"""
    from src.utils.junk_filter import is_junk

    title   = (row.get("title") or "").strip()
    yes_ask = float(row.get("yes_ask") or 0)
    volume  = float(row.get("volume") or 0)
    ct      = row.get("close_time") or ""
    hl      = _hours_left(ct)

    if hl < 0:
        return "CLOSED", "already closed"
    if not ct:
        return "SKIP", "no close_time"

    # Sub-market filter first — label correctly before resolving check
    title_l = title.lower()
    for pat in _SUBMARKET_SKIP:
        if pat in title_l:
            return "SKIP", f"sub-market: {pat[:18]}"

    if 0 <= hl < 0.5:
        return "SKIP", "resolving (<30m)"

    if is_junk(title):
        return "SKIP", "junk filter"
    if volume > 0 and volume < 50:
        return "SKIP", f"vol={volume:.0f}<50"
    if yes_ask > 0 and yes_ask < 15:
        return "SKIP", f"long-shot {yes_ask:.0f}c"
    if yes_ask > 0 and yes_ask > 95:
        return "SKIP", f"near-certain {yes_ask:.0f}c"
    return "PASS", ""


# ─────────────────────────────────────────────────────────────────────────────
# BID label
# ─────────────────────────────────────────────────────────────────────────────

def _bid_label(gate: str, bot_action: str, bot_conf: float, min_conf: float, hl: float) -> tuple:
    if gate == "CLOSED":
        return "CLOSED", "already closed"
    if gate == "SKIP":
        return "SKIP", ""

    if bot_action == "BUY" and bot_conf >= min_conf:
        return "BID YES", f"conf={bot_conf:.0f}%"
    if bot_action == "BUY" and bot_conf > 0:
        return "WATCH", f"BUY {bot_conf:.0f}%<{min_conf:.0f}%"
    if bot_action == "HOLD" and bot_conf > 0:
        return "WATCH", f"HOLD {bot_conf:.0f}%"

    needed = 70 if hl <= 6 else (75 if hl <= 24 else 88)
    return "WATCH", f"need {needed:.0f}% conf"


# ─────────────────────────────────────────────────────────────────────────────
# Table rendering
# ─────────────────────────────────────────────────────────────────────────────

def _trunc(s: str, w: int) -> str:
    s = (s or "").strip()
    return (s[: w - 1] + ">") if len(s) > w else s.ljust(w)


def _div(widths: list) -> str:
    return "+" + "+".join("-" * (w + 2) for w in widths) + "+"


def _hrow(cols: list, widths: list) -> str:
    return "|" + "|".join(f" {str(c).ljust(w)} " for c, w in zip(cols, widths)) + "|"


def _drow(vals: list, widths: list) -> str:
    return "|" + "|".join(f" {str(v).ljust(w)} " for v, w in zip(vals, widths)) + "|"


def _print_table(platform: str, rows: list, ai_map: dict, min_conf: float,
                 show_skip: bool, source: str) -> None:
    icon = "KALSHI" if platform == "kalshi" else "POLYMARKET"
    widths = _col_widths()
    term_w = sum(widths) + len(widths) * 3 + 1   # each col: " val " + "|"
    bar    = "=" * term_w
    print(f"\n{bar}")
    print(f"  [{icon}]  {source}  —  {_now_et().strftime('%a %b %d %Y %I:%M %p ET')}")
    print(bar)

    if not rows:
        print("  No markets found for today.\n")
        return

    cols = ["#", "TITLE", "CLOSES (ET)", "YES", "NO", "VOL", "CONF", "BID?", "REASON"]
    div  = _div(widths)

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
        title   = (row.get("title") or ticker or "").strip()

        ai       = ai_map.get(ticker) or {}
        bot_act  = (ai.get("action") or "").upper()
        bot_conf = float(ai.get("confidence") or 0)
        bot_rsn  = (ai.get("reasoning") or "").strip()

        bid, bid_reason = _bid_label(gate, bot_act, bot_conf, min_conf, hl)

        if gate == "SKIP":
            skip_count += 1
        elif bid == "BID YES":
            bid_count += 1
        elif bid == "WATCH":
            watch_count += 1

        shown += 1

        # Urgency marker
        close_raw = _fmt_close(ct)
        if gate == "PASS" and 0 < hl <= 1:
            close_str = "!!" + close_raw
        elif gate == "PASS" and 0 < hl <= 3:
            close_str = "! " + close_raw
        else:
            close_str = "  " + close_raw

        bid_col    = f"[{bid}]"
        reason_col = gate_reason if gate == "SKIP" else bid_reason
        conf_str   = f"{bot_conf:.0f}%" if bot_conf > 0 else "-"

        _nw, _tw, _cw, _yw, _now2, _vw, _cfw, _bw, _rw = widths
        vals = [
            str(i),
            _trunc(title, _tw),
            _trunc(close_str, _cw),
            f"{yes_ask:.0f}c" if yes_ask else "-",
            f"{no_ask:.0f}c"  if no_ask  else "-",
            f"{volume:.0f}"   if volume   else "-",
            conf_str,
            bid_col,
            _trunc(reason_col, _rw),
        ]
        print(_drow(vals, widths))

        # Bot reasoning on second line (only when evaluated)
        if bot_rsn and gate == "PASS":
            rsn_w = sum(widths) + len(widths) * 3 - 6
            print(f"     > {bot_rsn[:rsn_w]}")

    print(div)
    summary = f"  Shown: {shown}  | [BID] {bid_count}  | [WATCH] {watch_count}  | [SKIP] {skip_count}"
    if skip_count and not show_skip:
        summary += "  (run --all-markets to see skip reasons)"
    if shown == 0 and skip_count > 0:
        summary += "\n  ⚠  All markets filtered out — likely sub-markets or resolving soon."
        summary += " Run --all-markets to inspect, or check back later for new events."
    print(summary)
    print(bar)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_db(db_path: str) -> tuple:
    if not os.path.exists(db_path):
        return {}, {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()

    cur.execute(
        "SELECT ticker, title, close_time, yes_ask, no_ask, volume, "
        "       status, platform, last_price "
        "FROM markets WHERE (status='open' OR status='' OR status IS NULL) "
        "ORDER BY close_time ASC"
    )
    db_markets: dict = {"kalshi": [], "polymarket": []}
    for r in cur.fetchall():
        row  = dict(r)
        plat = (row.get("platform") or "kalshi").lower()
        if plat in db_markets:
            db_markets[plat].append(row)

    today_prefix = _now_et().date().isoformat()
    ai_map: dict = {}
    for table, a_col, c_col, r_col, t_col in [
        ("ai_decisions",  "action", "confidence",    "reasoning",    "decided_at"),
        ("paper_signals", "action", "ai_confidence", "ai_reasoning", "created_at"),
    ]:
        try:
            cur.execute(
                f"SELECT ticker, {a_col} AS action, {c_col} AS confidence, "
                f"       {r_col} AS reasoning, {t_col} AS decided_at "
                f"FROM {table} WHERE {t_col} >= ? ORDER BY {t_col} DESC",
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

async def _fetch_kalshi_live(days: int = 3) -> tuple:
    """Fetch Kalshi markets closing within the next `days` days."""
    try:
        from src.clients.kalshi_client import KalshiClient
        client = KalshiClient()

        today    = _now_et().date()
        end_date = today + timedelta(days=days - 1)

        all_raw: list = []
        cursor = ""
        for _ in range(20):                       # up to 20 pages × 200 = 4000 markets
            data   = await client.get_markets(limit=200, cursor=cursor, status="open")
            batch  = data.get("markets") or []
            cursor = data.get("cursor") or ""
            all_raw.extend(batch)
            if not cursor or not batch:
                break

        def _cents(v):
            """Convert Kalshi price to cents (handles fractions 0-1 or integers 1-99)."""
            try:
                f = float(v)
                return round(f * 100, 1) if 0 < f < 1.0 else round(f, 1)
            except (TypeError, ValueError):
                return 0.0

        result = []
        for m in all_raw:
            ct = m.get("close_time") or m.get("expiration_time") or ""
            dt = _parse_close(ct)
            if dt is None:
                continue
            if not (today <= dt.date() <= end_date):
                continue
            yes_ask = _cents(m.get("yes_ask") or m.get("last_price") or m.get("yes_bid") or 0)
            no_ask  = _cents(m.get("no_ask")  or m.get("no_bid")  or 0) or round(100 - yes_ask, 1)
            result.append({
                "ticker":     m.get("ticker", ""),
                "title":      m.get("title", ""),
                "close_time": ct,
                "yes_ask":    yes_ask,
                "no_ask":     no_ask if yes_ask else 0.0,
                "volume":     float(m.get("volume")  or 0),
                "status":     m.get("status", "open"),
                "platform":   "kalshi",
            })

        result.sort(key=lambda r: (r.get("close_time") or "", -float(r.get("volume") or 0)))
        return result, f"LIVE API — {len(result)} markets (from {len(all_raw)} total)"
    except Exception as e:
        return [], f"API error: {e}"


async def _fetch_poly_live(days: int = 3) -> tuple:
    """Fetch Polymarket markets closing within the next `days` days via Gamma API."""
    try:
        import httpx
        _now_et_dt  = _now_et()
        # Start of today ET → UTC so we get ALL of today's markets, not just future ones
        _sod_et     = _now_et_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        _eod_et     = _now_et_dt.replace(hour=23, minute=59, second=59) + timedelta(days=days - 1)
        _sod_utc    = _sod_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _eod_utc = _eod_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        GAMMA  = "https://gamma-api.polymarket.com"
        result = []
        seen   = set()
        offset = 0

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                r = await client.get(f"{GAMMA}/markets", params={
                    "active":       "true",
                    "limit":        500,
                    "offset":       offset,
                    "end_date_min": _sod_utc,
                    "end_date_max": _eod_utc,
                })
                if r.status_code != 200:
                    break

                raw   = r.json()
                items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])
                if not items:
                    break

                for m in items:
                    if not isinstance(m, dict):
                        continue

                    ct    = (m.get("endDate") or m.get("end_date") or
                             m.get("close_time") or m.get("closeTime") or "")
                    title = (m.get("question") or m.get("title") or "").strip()
                    if not title:
                        continue

                    # Deduplicate by title+close_time
                    key = (title.lower(), ct)
                    if key in seen:
                        continue
                    seen.add(key)

                    def _p(v):
                        try:
                            f = float(v)
                            return round(f * 100, 1) if 0 < f <= 1.0 else round(f, 1)
                        except (TypeError, ValueError):
                            return 0.0

                    # Parse yes/no prices from tokens list
                    yes_ask = no_ask = 0.0
                    tokens  = m.get("tokens") or []
                    for tok in tokens:
                        if not isinstance(tok, dict):
                            continue
                        name  = (tok.get("outcome") or tok.get("name") or "").lower()
                        price = _p(tok.get("price") or 0)
                        if "yes" in name:
                            yes_ask = price
                        elif "no" in name:
                            no_ask  = price

                    # Fallback: outcomePrices list ["0.65", "0.35"]
                    if yes_ask == 0:
                        op = m.get("outcomePrices") or []
                        if len(op) >= 2:
                            try:
                                yes_ask = _p(op[0])
                                no_ask  = _p(op[1])
                            except (ValueError, TypeError):
                                pass

                    # Final fallback: lastTradePrice
                    if yes_ask == 0:
                        yes_ask = _p(m.get("lastTradePrice") or m.get("last_trade_price") or 0)

                    # Derive no_ask from yes_ask if still missing
                    if yes_ask > 0 and no_ask == 0:
                        no_ask = round(100 - yes_ask, 1)

                    ticker = m.get("conditionId") or m.get("id") or ""
                    volume = 0.0
                    for vk in ("volume", "volumeNum", "volume24hr", "usdcVolume"):
                        try:
                            v = float(m.get(vk) or 0)
                            if v > volume:
                                volume = v
                        except (ValueError, TypeError):
                            pass

                    result.append({
                        "ticker":     ticker,
                        "title":      title,
                        "close_time": ct,
                        "yes_ask":    yes_ask,
                        "no_ask":     no_ask,
                        "volume":     volume,
                        "status":     "open",
                        "platform":   "polymarket",
                    })

                # Paginate if we got a full page
                if len(items) < 500:
                    break
                offset += 500

        result.sort(key=lambda r: (r.get("close_time") or "", -float(r.get("volume") or 0)))
        return result, f"LIVE API — {len(result)} markets"
    except Exception as e:
        return [], f"API error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def _run(args):
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

    db_markets, ai_map = _load_db(db_path) if db_path else ({}, {})
    if ai_map:
        print(f"  Bot evaluations in DB today: {len(ai_map)} tickers")
    else:
        print("  Bot evaluations in DB today: 0  (CONF will show '-' — bot hasn't evaluated these markets yet or is stopped)")

    platforms = ["kalshi", "polymarket"]
    if args.platform:
        platforms = [args.platform]

    for plat in platforms:
        markets = []
        source  = "DB cache"

        if not args.db_only:
            print(f"\n  Fetching {plat.upper()} from live API...", end="", flush=True)
            if plat == "kalshi":
                markets, source = await _fetch_kalshi_live(args.days)
            else:
                markets, source = await _fetch_poly_live(args.days)
            print(f" {source}")

        # Fallback to DB cache
        if not markets:
            cache     = db_markets.get(plat, [])
            cutoff    = _now_et().date() + timedelta(days=args.days - 1)
            filtered  = [
                r for r in cache
                if (lambda dt: dt is not None and _now_et().date() <= dt.date() <= cutoff)(
                    _parse_close(r.get("close_time") or "")
                )
            ]
            if filtered:
                markets = filtered
                source  = f"DB cache — {len(markets)} markets (bot stopped)"
            elif cache:
                dated   = [r for r in cache if r.get("close_time")]
                markets = sorted(dated, key=lambda r: r.get("close_time") or "")[:100]
                source  = f"DB cache STALE — bot stopped, last {len(markets)} known markets"
            else:
                source = "no data — restart bot to populate DB"

        _print_table(plat, markets, ai_map, args.min_conf, args.all_markets, source)

    print("  Tips:")
    print("    --all-markets       show junk/skipped rows with reason")
    print("    --days 3            today + next 2 days  |  --days 7  full week")
    print("    --platform kalshi | polymarket  one platform only")
    print("    --db-only           skip live API, use DB cache only")
    print("    --min-conf 80       change BID threshold (default 75)\n")


def main():
    parser = argparse.ArgumentParser(
        description="Show today's live events with bot confidence + BID/WATCH/SKIP"
    )
    parser.add_argument("--platform",    choices=["kalshi", "polymarket"])
    parser.add_argument("--days",        type=int, default=1,
                        help="Look-ahead window in days (default 1 = today only)")
    parser.add_argument("--all-markets", action="store_true", help="Show junk/skipped rows")
    parser.add_argument("--min-conf",    type=float, default=75.0)
    parser.add_argument("--db",          default=None, help="Path to trading_system.db")
    parser.add_argument("--db-only",     action="store_true", help="Skip live API, use DB only")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
