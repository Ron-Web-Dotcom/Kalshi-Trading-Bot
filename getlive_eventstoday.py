"""
getlive_eventstoday.py — fetch live markets from Kalshi + Polymarket APIs
and display them with bot confidence, BID/WATCH/SKIP labels.

Usage (on VPS):
  cd /root/trading-bot
  python3 getlive_eventstoday.py
  python3 getlive_eventstoday.py --days 3        # today + next 2 days
  python3 getlive_eventstoday.py --platform kalshi
  python3 getlive_eventstoday.py --platform polymarket
  python3 getlive_eventstoday.py --all-markets   # also show BOT SKIP rows
  python3 getlive_eventstoday.py --min-conf 80   # change BID threshold
  python3 getlive_eventstoday.py --db-only       # use DB cache only
"""

import argparse
import asyncio
import shutil
import sqlite3
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Load .env so API keys work when running standalone (bot not started)
def _load_dotenv():
    for candidate in [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.expanduser("~/trading-bot/.env"),
        "/root/trading-bot/.env",
    ]:
        if not os.path.exists(candidate):
            continue
        with open(candidate) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        break

_load_dotenv()

_ET = ZoneInfo("America/New_York")

# ── Column widths ─────────────────────────────────────────────────────────────
_FIXED_OVERHEAD = 78 + 26

def _col_widths(term_w: int = 0) -> tuple:
    if term_w <= 0:
        term_w = shutil.get_terminal_size(fallback=(120, 40)).columns
    title_w = max(28, min(60, term_w - _FIXED_OVERHEAD))
    return (3, title_w, 21, 5, 5, 8, 7, 9, 20)


# ── Sub-market patterns — gate filter keeps table clean ──────────────────────
_SUBMARKET_SKIP = [
    # Financial options / strike prices
    "target price:", "yes $", "no $", "or above,yes", "or above,no",
    "$0.0", "above,yes", "above,no",
    # Commodity / index close-price brackets (e.g. "Will GOLD close price be above $2300?")
    "close price be", "close price ab", "close price be",
    "close above $", "close below $", "settle above", "settle below",
    "price be above", "price be below", "price above $", "price below $",
    # Exact score
    "exact score:", "exact score ", "any other score",
    # Halftime / period splits
    "leading at halftime", "at halftime", "halftime",
    "to win the second half", "second half draw", "second half",
    "to win the first half", "first half",
    # In-game micro-markets
    "first goal", "first blood", "first dragon", "first baron", "first tower",
    "to score first", "red card", "yellow card",
    "corner kicks", "total goals",
    "both teams to score", "both teams",
    # Over/Under / spread / handicap
    "over/under", "o/u 0.", "o/u 1.", "o/u 2.", "o/u 3.", "o/u 4.", "o/u 5.",
    "spread:", "handicap", "asian handicap",
    # Polymarket soccer sub-market title patterns
    ": draw", ": 1st >", ": both", ": fk m", ": univ",
    "to win on penalties", "extra time",
    # Baseball / same-game parlay combos (e.g. "yes Caleb Durbin: 1+,yes Jonathan Aranda: 1+")
    "1+,yes", "1+,no", "2+,yes", "2+,no", "3+,yes", "3+,no",
    "+,yes ", "+,no ",
]


def _is_parlay_title(title: str) -> bool:
    """Detect titles that start with 'yes <team>,' or 'no <team>,' — parlay legs, not questions."""
    t = title.lower().strip()
    if (t.startswith("yes ") or t.startswith("no ")) and "," in t:
        # Real questions start with "will", "who", "what", "which", etc.
        # Parlay legs start with yes/no then a team/player name then comma
        return True
    return False


# ── Time helpers ──────────────────────────────────────────────────────────────

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


def _fmt_price(v: float) -> str:
    if not v:
        return "-"
    return "<1c" if v < 1 else f"{v:.0f}c"


# ── Gate check — display filter ───────────────────────────────────────────────

def _gate_check(row: dict) -> tuple:
    """PASS / SKIP / CLOSED — only filters sub-markets, closed, resolving <5m."""
    ct = row.get("close_time") or ""
    if not ct:
        return "SKIP", "no close_time"

    title_l = (row.get("title") or "").lower()
    if _is_parlay_title(title_l):
        return "SKIP", "parlay-combo"
    for pat in _SUBMARKET_SKIP:
        if pat in title_l:
            return "SKIP", "sub-market"

    hl = _hours_left(ct)
    if hl < 0:
        return "CLOSED", "already closed"
    if 0 <= hl < 0.083:
        return "SKIP", "resolving (<5m)"

    return "PASS", ""


# ── BID label — trading filter ────────────────────────────────────────────────

def _bid_label(gate: str, bot_action: str, bot_conf: float, min_conf: float,
               row: dict) -> tuple:
    if gate in ("CLOSED", "SKIP"):
        return gate, ""

    title   = (row.get("title") or "").strip()
    yes_ask = float(row.get("yes_ask") or 0)
    no_ask  = float(row.get("no_ask")  or 0)
    volume  = float(row.get("volume")  or 0)

    # No price and no volume = nothing to show or trade
    if yes_ask == 0 and no_ask == 0 and volume == 0:
        return "BOT SKIP", "no price/volume"
    if volume > 0 and volume < 20:
        return "BOT SKIP", f"vol={volume:.0f}<20"
    if yes_ask > 0 and yes_ask > 97:
        return "BOT SKIP", f"near-certain {yes_ask:.0f}c"
    # Long-shot: very cheap price + thin volume (e.g. NFL draft picks at 1-4c / 44-140 vol)
    if yes_ask > 0 and yes_ask < 5 and volume < 500:
        return "BOT SKIP", f"long-shot {yes_ask:.0f}c vol={volume:.0f}"

    if bot_action == "BUY" and bot_conf >= min_conf:
        return "BID YES", f"conf={bot_conf:.0f}%"
    if bot_action == "BUY" and bot_conf > 0:
        return "WATCH", f"BUY {bot_conf:.0f}%<{min_conf:.0f}%"
    if bot_action == "HOLD" and bot_conf > 0:
        return "WATCH", f"HOLD {bot_conf:.0f}%"

    return "WATCH", "not evaluated"


# ── Table rendering ───────────────────────────────────────────────────────────

def _trunc(s: str, w: int) -> str:
    s = (s or "").strip()
    return (s[: w - 1] + ">") if len(s) > w else s.ljust(w)


def _div(widths):
    return "+" + "+".join("-" * (w + 2) for w in widths) + "+"


def _hrow(cols, widths):
    return "|" + "|".join(f" {str(c).ljust(w)} " for c, w in zip(cols, widths)) + "|"


def _drow(vals, widths):
    return "|" + "|".join(f" {str(v).ljust(w)} " for v, w in zip(vals, widths)) + "|"


def _print_table(platform: str, rows: list, ai_map: dict, min_conf: float,
                 show_all: bool, source: str, bot_active: bool) -> None:
    icon   = "KALSHI" if platform == "kalshi" else "POLYMARKET"
    widths = _col_widths()
    term_w = sum(widths) + len(widths) * 3 + 1
    bar    = "=" * term_w

    print(f"\n{bar}")
    print(f"  [{icon}]  {source}  —  {_now_et().strftime('%a %b %d %Y %I:%M %p ET')}")
    print(bar)

    if not rows:
        print("  No markets returned from API.\n")
        print(bar)
        return

    # Ticker dedup — removes same market returned twice across API pages
    seen: set = set()
    deduped   = []
    for r in rows:
        key = r.get("ticker") or r.get("title", "")
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    rows = deduped

    cols = ["#", "TITLE", "CLOSES (ET)", "YES", "NO", "VOL", "CONF", "BID?", "REASON"]
    div  = _div(widths)
    print(div)
    print(_hrow(cols, widths))
    print(div)

    bid_count = watch_count = skip_count = bot_skip_count = shown = 0
    display_num = 0

    for row in rows:
        gate, gate_reason = _gate_check(row)

        if gate in ("CLOSED", "SKIP"):
            skip_count += 1
            if show_all:
                # Show skipped rows when --all-markets
                display_num += 1
                ct2    = row.get("close_time") or ""
                title2 = (row.get("title") or row.get("ticker") or "").strip()
                vals   = [
                    str(display_num),
                    _trunc(title2, widths[1]),
                    _trunc("  " + _fmt_close(ct2), widths[2]),
                    "-", "-", "-", "-",
                    f"[{gate}]",
                    _trunc(gate_reason, widths[8]),
                ]
                print(_drow(vals, widths))
            continue

        ct      = row.get("close_time") or ""
        ticker  = row.get("ticker") or ""
        yes_ask = float(row.get("yes_ask") or 0)
        no_ask  = float(row.get("no_ask")  or 0)
        volume  = float(row.get("volume")  or 0)
        hl      = _hours_left(ct)
        title   = (row.get("title") or ticker or "").strip()

        # Only use rule engine when bot is active (has evaluations in DB)
        ai = ai_map.get(ticker) or {}
        if not ai and bot_active:
            try:
                from src.ai.rule_engine import score as _rs
                rd = _rs(row, context="", manifold_text=None, metaculus_text=None)
                ai = {"action": rd.action, "confidence": rd.confidence}
            except Exception:
                pass

        bot_act  = (ai.get("action") or "").upper()
        bot_conf = float(ai.get("confidence") or 0)

        bid, bid_reason = _bid_label(gate, bot_act, bot_conf, min_conf, row)

        if bid == "BOT SKIP":
            bot_skip_count += 1
            if not show_all:
                continue
        elif bid == "BID YES":
            bid_count += 1
        elif bid == "WATCH":
            watch_count += 1

        display_num += 1
        shown += 1

        hl = _hours_left(ct)
        close_raw = _fmt_close(ct)
        if 0 < hl <= 1:
            close_str = "!!" + close_raw
        elif 0 < hl <= 3:
            close_str = "! " + close_raw
        else:
            close_str = "  " + close_raw

        conf_str = f"{bot_conf:.0f}%" if bot_conf > 0 else "-"

        _nw, _tw, _cw, _yw, _now2, _vw, _cfw, _bw, _rw = widths
        vals = [
            str(display_num),
            _trunc(title, _tw),
            _trunc(close_str, _cw),
            _fmt_price(yes_ask),
            _fmt_price(no_ask),
            f"{volume:.0f}" if volume else "-",
            conf_str,
            f"[{bid}]",
            _trunc(bid_reason, _rw),
        ]
        print(_drow(vals, widths))

    print(div)
    print(f"  Shown: {shown}  | [BID] {bid_count}  | [WATCH] {watch_count}"
          f"  | [BOT SKIP] {bot_skip_count}  | [SKIP] {skip_count}")

    if skip_count > 20 and not show_all:
        reason_counts: dict = {}
        for row in rows:
            g2, r2 = _gate_check(row)
            if g2 in ("SKIP", "CLOSED"):
                reason_counts[r2] = reason_counts.get(r2, 0) + 1
        if reason_counts:
            top = sorted(reason_counts.items(), key=lambda x: -x[1])
            print(f"  ℹ  Filtered: { {k: v for k, v in top} }"
                  f"  — run --all-markets to inspect")

    if bot_skip_count > 20 and not show_all:
        # Sample the BOT SKIP reasons so user can see what's being hidden
        bot_reason_counts: dict = {}
        bot_samples = []
        for row in rows:
            g2, _ = _gate_check(row)
            if g2 not in ("CLOSED", "SKIP"):
                b2, r2 = _bid_label(g2, "", 0, min_conf, row)
                if b2 == "BOT SKIP":
                    bot_reason_counts[r2] = bot_reason_counts.get(r2, 0) + 1
                    if len(bot_samples) < 3:
                        bot_samples.append(f"{(row.get('title') or '')[:45]} [{r2}]")
        if bot_reason_counts:
            top = sorted(bot_reason_counts.items(), key=lambda x: -x[1])
            print(f"  ℹ  BOT SKIP breakdown: { {k: v for k, v in top} }")
            for s in bot_samples:
                print(f"       e.g. {s}")

    if shown == 0 and not show_all:
        if bot_skip_count > 0:
            print(f"  ℹ  {bot_skip_count} markets exist but bot won't trade them"
                  f" — run --all-markets to see")
        elif skip_count == 0:
            print(f"  ℹ  No markets returned — try --days 3")

    print(bar)
    print()


# ── DB helpers ────────────────────────────────────────────────────────────────

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


# ── Live API fetchers ─────────────────────────────────────────────────────────

async def _fetch_kalshi_live(days: int = 2) -> tuple:
    """
    Fetch Kalshi markets two ways and merge:
      1. get_live_now_markets() — sports/events happening RIGHT NOW (soccer,
         NBA, esports, etc.) via the /events endpoint + sport series tickers.
         This is what the user sees on Kalshi's LIVE badge.
      2. get_markets() pagination — regular markets (crypto, political, finance)
         filtered to the close-date window.
    """
    try:
        from src.clients.kalshi_client import KalshiClient
        client = KalshiClient()

        today    = _now_et().date()
        end_date = today + timedelta(days=days - 1)

        def _cents(v):
            try:
                f = float(v)
                return round(f * 100, 1) if 0 < f < 1.0 else round(f, 1)
            except (TypeError, ValueError):
                return 0.0

        def _norm(m: dict) -> dict:
            ct = m.get("close_time") or m.get("expiration_time") or ""
            yes_ask = _cents(m.get("yes_ask") or m.get("last_price") or m.get("yes_bid") or 0)
            no_ask  = _cents(m.get("no_ask") or m.get("no_bid") or 0) or round(100 - yes_ask, 1)
            return {
                "ticker":     m.get("ticker", ""),
                "title":      m.get("title", ""),
                "close_time": ct,
                "yes_ask":    yes_ask,
                "no_ask":     no_ask if yes_ask else 0.0,
                "volume":     float(m.get("volume") or 0),
                "status":     "open",
                "platform":   "kalshi",
            }

        # ── Pass 1: live sports/events ────────────────────────────────────────
        live_raw = await client.get_live_now_markets(max_markets=500)
        live_result = []
        for m in live_raw:
            ct = m.get("close_time") or ""
            dt = _parse_close(ct)
            if dt is None:
                continue
            live_result.append(_norm(m))

        # ── Pass 2: open + live markets, wider window (7 days) ───────────────
        # Kalshi uses status=open for pre-match and status=live for in-play.
        # World Cup / tournament markets can close up to 7 days out so we
        # always look at least 7 days ahead regardless of --days flag.
        kalshi_end = today + timedelta(days=max(days - 1, 6))

        all_raw: list = []
        cursor = ""
        for _ in range(20):
            data   = await client.get_markets(limit=200, cursor=cursor, status="open",
                                              sort_by="close_time", order="asc")
            batch  = data.get("markets") or []
            cursor = data.get("cursor") or ""
            all_raw.extend(batch)
            if not cursor or not batch:
                break

        date_result = []
        seen_date: set = set()
        for m in all_raw:
            ct = m.get("close_time") or m.get("expiration_time") or ""
            dt = _parse_close(ct)
            if dt is None or dt.date() < today or dt.date() > kalshi_end:
                continue
            t = m.get("ticker", "")
            if t in seen_date:
                continue
            seen_date.add(t)
            date_result.append(_norm(m))

        # ── Merge: live markets first, then date-filtered, dedup by ticker ────
        merged: dict = {}
        for r in live_result + date_result:
            t = r.get("ticker") or r.get("title", "")
            if t and t not in merged:
                merged[t] = r

        result = sorted(merged.values(),
                        key=lambda r: (r.get("close_time") or "", -float(r.get("volume") or 0)))
        return result, (f"LIVE API — {len(result)} markets"
                        f" ({len(live_result)} live-now + {len(date_result)} closing ≤7d)")
    except Exception as e:
        return [], f"API error: {e}"


async def _fetch_poly_live(days: int = 2) -> tuple:
    """Fetch Polymarket markets closing within the next `days` days via Gamma API."""
    try:
        import httpx
        _now_et_dt = _now_et()
        _today     = _now_et_dt.date()
        _end_date  = _today + timedelta(days=days - 1)
        _sod_et    = _now_et_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        _eod_et    = _now_et_dt.replace(hour=23, minute=59, second=59) + timedelta(days=days - 1)
        _sod_utc   = _sod_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _eod_utc   = _eod_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        GAMMA  = "https://gamma-api.polymarket.com"
        result = []
        seen   = set()
        offset = 0

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                r = await client.get(f"{GAMMA}/markets", params={
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

                    _dt = _parse_close(ct)
                    if _dt is None or not (_today <= _dt.date() <= _end_date):
                        continue

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

                    yes_ask = no_ask = 0.0
                    for tok in (m.get("tokens") or []):
                        if not isinstance(tok, dict):
                            continue
                        name  = (tok.get("outcome") or tok.get("name") or "").lower()
                        price = _p(tok.get("price") or 0)
                        if "yes" in name:
                            yes_ask = price
                        elif "no" in name:
                            no_ask  = price

                    if yes_ask == 0:
                        op = m.get("outcomePrices") or []
                        if len(op) >= 2:
                            try:
                                yes_ask = _p(op[0])
                                no_ask  = _p(op[1])
                            except (ValueError, TypeError):
                                pass

                    if yes_ask == 0:
                        yes_ask = _p(m.get("lastTradePrice") or m.get("last_trade_price") or 0)

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

                if len(items) < 500:
                    break
                offset += 500

        result.sort(key=lambda r: (r.get("close_time") or "", -float(r.get("volume") or 0)))
        return result, f"LIVE API — {len(result)} markets"
    except Exception as e:
        return [], f"API error: {e}"


# ── Main ──────────────────────────────────────────────────────────────────────

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
    bot_active = bool(ai_map)
    if bot_active:
        print(f"  Bot evaluations in DB today: {len(ai_map)} tickers")
    else:
        print("  Bot evaluations: 0  (bot stopped — CONF shows '-', run bot for AI scoring)")

    platforms = ["kalshi", "polymarket"]
    if args.platform:
        platforms = [args.platform]

    for plat in platforms:
        markets = []
        source  = "DB cache"
        api_ok  = False

        if not args.db_only:
            print(f"\n  Fetching {plat.upper()} from live API...", end="", flush=True)
            if plat == "kalshi":
                markets, source = await _fetch_kalshi_live(args.days)
            else:
                markets, source = await _fetch_poly_live(args.days)

            api_ok = "API error" not in source and "error" not in source.lower()
            print(f" {source}")
            if not api_ok:
                print(f"  ⚠  {plat.upper()} live API failed — check .env + network")

        if not markets:
            cache  = db_markets.get(plat, [])
            today  = _now_et().date()
            cutoff = today + timedelta(days=args.days - 1)
            filtered = [
                r for r in cache
                if (lambda dt: dt is not None and today <= dt.date() <= cutoff)(
                    _parse_close(r.get("close_time") or "")
                )
            ]
            if filtered:
                markets = filtered
                source  = f"DB cache — {len(markets)} markets"
            else:
                markets = []
                source  = "no markets in cache — " + (
                    "live API failed; check .env + network" if not api_ok and not args.db_only
                    else "start the bot so it can fetch events"
                )

        _print_table(plat, markets, ai_map, args.min_conf, args.all_markets, source, bot_active)

    print("  Tips:")
    print("    --all-markets       show BOT SKIP rows too")
    print("    --days 3            today + next 2 days  |  --days 7  full week")
    print("    --platform kalshi | polymarket")
    print("    --min-conf 80       change BID threshold (default 75)\n")


def main():
    parser = argparse.ArgumentParser(
        description="Show live events with bot confidence + BID/WATCH/SKIP"
    )
    parser.add_argument("--platform",    choices=["kalshi", "polymarket"])
    parser.add_argument("--days",        type=int, default=2,
                        help="Look-ahead window (default 2 = today + tomorrow)")
    parser.add_argument("--all-markets", action="store_true",
                        help="Show BOT SKIP and SKIP rows")
    parser.add_argument("--min-conf",    type=float, default=75.0)
    parser.add_argument("--db",          default=None)
    parser.add_argument("--db-only",     action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
