"""
getlive_eventstoday.py — show all live events closing today (Eastern time)
for both Kalshi and Polymarket, with bot confidence, reasoning, and BID/WATCH/SKIP.

Usage (on VPS):
  cd /root/trading-bot
  python3 getlive_eventstoday.py
  python3 getlive_eventstoday.py --all           # include tomorrow's markets too
  python3 getlive_eventstoday.py --platform kalshi
  python3 getlive_eventstoday.py --platform polymarket
  python3 getlive_eventstoday.py --all-markets   # show junk/skipped rows too
  python3 getlive_eventstoday.py --min-conf 80   # change confidence threshold
"""

import argparse
import sqlite3
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# ── Column widths ─────────────────────────────────────────────────────────────
_TITLE_W   = 48
_CLOSE_W   = 28
_PRICE_W   = 6
_VOL_W     = 7
_CONF_W    = 8
_BID_W     = 11
_REASON_W  = 26
_NOTES_W   = 38


def _now_et() -> datetime:
    return datetime.now(_ET)


def _parse_close(ct: str):
    """Parse ISO close_time string → datetime in ET. Returns None on failure."""
    if not ct:
        return None
    try:
        dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_ET)
    except Exception:
        return None


def _hours_left(ct_str: str) -> float:
    """Hours until close. Negative = already closed. inf = unknown."""
    dt = _parse_close(ct_str)
    if dt is None:
        return float("inf")
    return (dt - _now_et()).total_seconds() / 3600


def _closes_today(ct_str: str) -> bool:
    dt = _parse_close(ct_str)
    if dt is None:
        return False
    return dt.date() == _now_et().date()


def _closes_tomorrow(ct_str: str) -> bool:
    dt = _parse_close(ct_str)
    if dt is None:
        return False
    return dt.date() == (_now_et() + timedelta(days=1)).date()


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


def _gate_check(row: dict) -> tuple[str, str]:
    """
    Run the same pre-AI quality gates as the trade engine.
    Returns (gate_result, reason):
      gate_result: "PASS" | "SKIP" | "CLOSED"
    """
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
        return "SKIP", f"long-shot {yes_ask:.0f}¢ < 15¢"
    if yes_ask > 0 and yes_ask > 95:
        return "SKIP", f"near-certain {yes_ask:.0f}¢ > 95¢"
    return "PASS", ""


def _bid_label(gate: str, bot_action: str, bot_conf: float, min_conf: float, hl: float) -> tuple[str, str]:
    """
    Determine the final BID / WATCH / SKIP label and short reason.
    gate        : "PASS" | "SKIP" | "CLOSED"
    bot_action  : "BUY" | "HOLD" | "" (no bot evaluation yet)
    bot_conf    : bot confidence 0–100 (0 if not evaluated)
    """
    if gate == "CLOSED":
        return "🔒 CLOSED", "already closed"
    if gate == "SKIP":
        return "⛔ SKIP", ""   # reason already in gate_reason column

    # Gate passed — now check bot evaluation
    if bot_action == "BUY" and bot_conf >= min_conf:
        return "✅ BID YES", f"conf={bot_conf:.0f}%"
    if bot_action == "BUY" and bot_conf > 0:
        return "👀 WATCH", f"BUY but conf={bot_conf:.0f}% < {min_conf:.0f}%"
    if bot_action == "HOLD" and bot_conf > 0:
        return "👀 WATCH", f"HOLD conf={bot_conf:.0f}%"
    if bot_action == "" or bot_conf == 0:
        # Not evaluated yet — show watching with required threshold
        if hl <= 6:
            needed = 70
        elif hl <= 24:
            needed = 75
        else:
            needed = 88
        return "👀 WATCH", f"not evaluated (need {needed:.0f}%)"

    return "👀 WATCH", f"conf={bot_conf:.0f}%"


def _truncate(s: str, w: int) -> str:
    s = (s or "").strip()
    if len(s) > w:
        return s[: w - 1] + "…"
    return s.ljust(w)


def _divider(widths: list) -> str:
    return "+" + "+".join("-" * (w + 2) for w in widths) + "+"


def _hrow(cols: list, widths: list) -> str:
    return "|" + "|".join(f" {str(c).ljust(w)} " for c, w in zip(cols, widths)) + "|"


def _drow(vals: list, widths: list) -> str:
    return "|" + "|".join(f" {str(v).ljust(w)} " for v, w in zip(vals, widths)) + "|"


def _print_table(
    platform: str,
    rows: list,
    ai_map: dict,
    min_conf: float,
    show_skip: bool,
) -> None:
    icon = "🟦 KALSHI" if platform == "kalshi" else "🟣 POLYMARKET"
    print(f"\n{'═'*120}")
    print(f"  {icon}  —  Live Events Closing Today  —  {_now_et().strftime('%A %b %d %Y %I:%M %p ET')}")
    print(f"{'═'*120}")

    if not rows:
        print("  No markets found.\n")
        return

    cols = [
        "#", "TITLE", "CLOSES (ET)",
        "YES", "NO", "VOL",
        "BOT CONF", "BID?", "GATE / REASON",
        "BOT REASONING (latest)",
    ]
    widths = [3, _TITLE_W, _CLOSE_W, _PRICE_W, _PRICE_W, _VOL_W,
              _CONF_W, _BID_W, _REASON_W, _NOTES_W]
    div = _divider(widths)

    print(div)
    print(_hrow(cols, widths))
    print(div)

    bid_count   = 0
    watch_count = 0
    skip_count  = 0
    shown       = 0

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

        # Pull latest bot evaluation for this ticker
        ai      = ai_map.get(ticker) or {}
        bot_act  = (ai.get("action") or "").upper()
        bot_conf = float(ai.get("confidence") or 0)
        bot_rsn  = (ai.get("reasoning") or "").strip()

        bid_label, bid_reason = _bid_label(gate, bot_act, bot_conf, min_conf, hl)

        # Counts
        if gate == "SKIP":
            skip_count += 1
        elif "BID YES" in bid_label:
            bid_count += 1
        elif "WATCH" in bid_label:
            watch_count += 1
        elif "CLOSED" in bid_label:
            pass

        shown += 1

        # Urgency prefix on close col
        if gate != "CLOSED" and 0 < hl <= 1:
            close_str = f"⚡ {_fmt_close(ct)}"
        elif gate != "CLOSED" and 0 < hl <= 3:
            close_str = f"🔴 {_fmt_close(ct)}"
        else:
            close_str = f"   {_fmt_close(ct)}"

        # Gate/reason column — show gate reason if skip, else bid_reason
        reason_col = gate_reason if gate == "SKIP" else bid_reason

        # Bot conf display
        if bot_conf > 0:
            conf_str = f"{bot_conf:.0f}%"
        else:
            conf_str = "—"

        vals = [
            str(i),
            _truncate(row.get("title") or ticker, _TITLE_W),
            _truncate(close_str, _CLOSE_W),
            f"{yes_ask:.0f}¢" if yes_ask else "—",
            f"{no_ask:.0f}¢"  if no_ask  else "—",
            f"{volume:.0f}"   if volume   else "—",
            conf_str.ljust(_CONF_W),
            _truncate(bid_label, _BID_W),
            _truncate(reason_col, _REASON_W),
            _truncate(bot_rsn, _NOTES_W),
        ]
        print(_drow(vals, widths))

    print(div)
    print(
        f"  Shown: {shown}  |  ✅ {bid_count} BID  |  👀 {watch_count} WATCH  "
        f"|  ⛔ {skip_count} SKIP"
        + ("  (use --all-markets to show skipped)" if skip_count and not show_skip else "")
    )
    print()


def _load_db(db_path: str) -> tuple[list, dict]:
    """
    Returns:
      markets  : list of market row dicts
      ai_map   : {ticker: latest ai_decision row} for today
    """
    if not os.path.exists(db_path):
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Markets
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, title, close_time, yes_ask, no_ask, volume, "
        "       open_interest, status, platform, last_price "
        "FROM markets "
        "WHERE (status='open' OR status='' OR status IS NULL) "
        "ORDER BY close_time ASC"
    )
    markets = [dict(r) for r in cur.fetchall()]

    # Latest AI decision per ticker (today only — pick most recent)
    today_prefix = _now_et().date().isoformat()
    cur.execute(
        "SELECT ticker, action, confidence, reasoning, model, decided_at "
        "FROM ai_decisions "
        "WHERE decided_at >= ? "
        "ORDER BY decided_at DESC",
        (today_prefix + "T00:00:00",)
    )
    ai_map: dict = {}
    for row in cur.fetchall():
        r = dict(row)
        t = r["ticker"]
        if t not in ai_map:          # first row = most recent (ORDER BY DESC)
            ai_map[t] = r

    # Also pull from paper_signals for markets evaluated but not in ai_decisions
    cur.execute(
        "SELECT ticker, action, ai_confidence AS confidence, ai_reasoning AS reasoning, "
        "       created_at AS decided_at "
        "FROM paper_signals "
        "WHERE created_at >= ? "
        "ORDER BY created_at DESC",
        (today_prefix + "T00:00:00",)
    )
    for row in cur.fetchall():
        r = dict(row)
        t = r["ticker"]
        if t not in ai_map:
            ai_map[t] = r

    conn.close()
    return markets, ai_map


def main():
    parser = argparse.ArgumentParser(
        description="Show today's live events with bot confidence + BID/WATCH/SKIP"
    )
    parser.add_argument("--platform",    choices=["kalshi", "polymarket"],
                        help="Show only one platform")
    parser.add_argument("--all",         action="store_true",
                        help="Include tomorrow's markets too")
    parser.add_argument("--all-markets", action="store_true",
                        help="Show junk/skipped markets too")
    parser.add_argument("--min-conf",    type=float, default=75.0,
                        help="Min confidence to show BID YES (default 75)")
    parser.add_argument("--db",          default=None,
                        help="Path to trading_system.db")
    args = parser.parse_args()

    # Resolve DB path
    db_path = args.db
    if db_path is None:
        candidates = [
            os.path.join(os.path.dirname(__file__), "trading_system.db"),
            os.path.expanduser("~/trading-bot/trading_system.db"),
            "/root/trading-bot/trading_system.db",
            "trading_system.db",
        ]
        for c in candidates:
            if os.path.exists(c):
                db_path = c
                break
    if db_path is None:
        print("ERROR: Cannot find trading_system.db. Pass --db /path/to/db", file=sys.stderr)
        sys.exit(1)

    print(f"\n  DB : {db_path}")
    print(f"  Now: {_now_et().strftime('%A %B %d %Y %I:%M:%S %p ET')}")

    markets, ai_map = _load_db(db_path)
    evaluated = sum(1 for t in ai_map if any(m["ticker"] == t for m in markets))
    print(f"  Bot evaluations loaded today: {len(ai_map)} tickers ({evaluated} match open markets)")

    def _filter(rows, platform):
        out = []
        for r in rows:
            p = (r.get("platform") or "kalshi").lower()
            if p != platform:
                continue
            ct = r.get("close_time") or ""
            if args.all:
                if not (_closes_today(ct) or _closes_tomorrow(ct)):
                    continue
            else:
                if not _closes_today(ct):
                    continue
            out.append(r)
        return sorted(out, key=lambda r: r.get("close_time") or "")

    platforms = ["kalshi", "polymarket"]
    if args.platform:
        platforms = [args.platform]

    for plat in platforms:
        filtered = _filter(markets, plat)
        _print_table(plat, filtered, ai_map, args.min_conf, args.all_markets)

    today_total = sum(
        1 for r in markets
        if _closes_today(r.get("close_time") or "")
        and (not args.platform
             or (r.get("platform") or "kalshi").lower() == args.platform)
    )
    print(f"  📊 Total markets closing today: {today_total}")
    print("  💡 --all-markets   → show junk/skip rows")
    print("  💡 --all           → include tomorrow")
    print("  💡 --platform kalshi | polymarket")
    print("  💡 --min-conf 80   → change BID threshold\n")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
