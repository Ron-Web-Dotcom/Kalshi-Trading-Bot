"""
getlive_eventstoday.py — show all live events closing today (Eastern time)
for both Kalshi and Polymarket, with bid/watch recommendation.

Usage (on VPS):
  cd /root/trading-bot
  python3 getlive_eventstoday.py
  python3 getlive_eventstoday.py --all        # include tomorrow's markets too
  python3 getlive_eventstoday.py --platform kalshi
  python3 getlive_eventstoday.py --platform polymarket
  python3 getlive_eventstoday.py --min-conf 80
"""

import argparse
import sqlite3
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# ── Column widths ─────────────────────────────────────────────────────────────
_TITLE_W   = 52
_CLOSE_W   = 17
_PRICE_W   = 9
_VOL_W     = 8
_STATUS_W  = 10
_ACTION_W  = 12


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
    """Hours until close. Negative = already closed."""
    dt = _parse_close(ct_str)
    if dt is None:
        return float("inf")
    return (dt - _now_et()).total_seconds() / 3600


def _closes_within_hours(ct_str: str, hours: float) -> bool:
    hl = _hours_left(ct_str)
    return 0 <= hl <= hours


def _closes_today(ct_str: str) -> bool:
    dt = _parse_close(ct_str)
    if dt is None:
        return False
    today_et = _now_et().date()
    return dt.date() == today_et


def _closes_tomorrow(ct_str: str) -> bool:
    dt = _parse_close(ct_str)
    if dt is None:
        return False
    tomorrow_et = (_now_et() + timedelta(days=1)).date()
    return dt.date() == tomorrow_et


def _action(row: dict, min_conf: float) -> tuple[str, str]:
    """
    Returns (action_label, reason) based on junk filter + price + volume rules.
    Mirrors the bot's decision gates without calling the AI.
    """
    from src.utils.junk_filter import is_junk

    title    = row.get("title") or ""
    yes_ask  = float(row.get("yes_ask") or 0)
    volume   = float(row.get("volume") or 0)
    ct       = row.get("close_time") or ""
    hl       = _hours_left(ct)

    if is_junk(title):
        return "SKIP", "junk filter"
    if volume > 0 and volume < 50:
        return "SKIP", f"vol={volume:.0f}<50"
    if yes_ask > 0 and yes_ask < 15:
        return "SKIP", f"long-shot {yes_ask:.0f}¢"
    if yes_ask > 0 and yes_ask > 95:
        return "SKIP", f"near-certain {yes_ask:.0f}¢"
    if hl < 0:
        return "CLOSED", "already closed"
    if 0 <= hl < 0.5:
        return "SKIP", "resolving now"
    if "up or down" in title.lower():
        return "SKIP", "up/down scalp"

    # Confidence threshold by time window
    if hl <= 6:
        min_c = 70
    elif hl <= 24:
        min_c = 75
    else:
        min_c = 88

    if min_c > min_conf:
        min_c = min_conf

    return "WATCH", f"≥{min_c:.0f}% conf needed"


def _fmt_title(title: str, w: int) -> str:
    title = (title or "").strip()
    if len(title) > w:
        return title[: w - 1] + "…"
    return title.ljust(w)


def _fmt_close(ct_str: str) -> str:
    dt = _parse_close(ct_str)
    if dt is None:
        return "unknown".ljust(_CLOSE_W)
    hl = _hours_left(ct_str)
    if hl < 0:
        label = "CLOSED"
    elif hl < 1:
        label = f"{hl*60:.0f}m left"
    else:
        label = f"{hl:.1f}h left"
    time_str = dt.strftime("%m/%d %I:%M%p ET")
    return f"{time_str} ({label})".ljust(_CLOSE_W + 12)


def _divider(widths: list[int]) -> str:
    return "+" + "+".join("-" * (w + 2) for w in widths) + "+"


def _header_row(cols: list[str], widths: list[int]) -> str:
    return "|" + "|".join(f" {c.ljust(w)} " for c, w in zip(cols, widths)) + "|"


def _data_row(vals: list[str], widths: list[int]) -> str:
    return "|" + "|".join(f" {str(v).ljust(w)} " for v, w in zip(vals, widths)) + "|"


def _print_table(platform: str, rows: list[dict], min_conf: float, show_skip: bool) -> None:
    icon = "🟦 KALSHI" if platform == "kalshi" else "🟣 POLYMARKET"
    print(f"\n{'═'*100}")
    print(f"  {icon}  —  Live Events Closing Today (Eastern)  —  {_now_et().strftime('%A %b %d %Y %I:%M %p ET')}")
    print(f"{'═'*100}")

    if not rows:
        print("  No markets found.\n")
        return

    cols   = ["#", "TITLE", "CLOSES (ET)", "YES", "NO", "VOL", "STATUS", "ACTION", "REASON"]
    widths = [3, _TITLE_W, 29, _PRICE_W, _PRICE_W, _VOL_W, _STATUS_W, _ACTION_W, 22]
    div    = _divider(widths)

    print(div)
    print(_header_row(cols, widths))
    print(div)

    bid_count  = 0
    watch_count = 0
    skip_count  = 0
    shown      = 0

    for i, row in enumerate(rows, 1):
        action, reason = _action(row, min_conf)

        if action == "SKIP" and not show_skip:
            skip_count += 1
            continue
        if action == "WATCH":
            watch_count += 1
        elif action in ("BID", "WATCH"):
            bid_count += 1
        elif action == "SKIP":
            skip_count += 1

        shown += 1
        ct      = row.get("close_time") or ""
        yes_ask = float(row.get("yes_ask") or 0)
        no_ask  = float(row.get("no_ask")  or 0)
        volume  = float(row.get("volume")  or 0)
        status  = (row.get("status") or "open").lower()
        hl      = _hours_left(ct)

        # Action emoji
        if action == "WATCH":
            act_label = "👀 WATCH"
        elif action == "CLOSED":
            act_label = "🔒 CLOSED"
        else:
            act_label = "⛔ SKIP"

        # Urgency flag on close time
        if 0 < hl <= 1:
            close_label = f"⚡ {_fmt_close(ct)}"
        elif 0 < hl <= 3:
            close_label = f"🔴 {_fmt_close(ct)}"
        else:
            close_label = f"   {_fmt_close(ct)}"

        vals = [
            str(i),
            _fmt_title(row.get("title") or row.get("ticker") or "", _TITLE_W),
            close_label[:31],
            f"{yes_ask:.0f}¢" if yes_ask else "—",
            f"{no_ask:.0f}¢"  if no_ask  else "—",
            f"{volume:.0f}"   if volume   else "—",
            status[:_STATUS_W],
            act_label[:_ACTION_W],
            reason[:22],
        ]
        print(_data_row(vals, widths))

    print(div)
    skipped_msg = f"  + {skip_count} junk/skip markets hidden (use --all-markets to show)" if skip_count and not show_skip else ""
    print(f"  Total: {shown} shown  |  👀 {watch_count} WATCHING  |  ⛔ {skip_count} skipped{('  ' + skipped_msg) if skipped_msg else ''}")
    print()


def _load_db(db_path: str) -> list[dict]:
    """Load all open markets from SQLite synchronously."""
    if not os.path.exists(db_path):
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()
    cur.execute(
        "SELECT ticker, title, close_time, yes_ask, no_ask, volume, "
        "       open_interest, status, platform, last_price "
        "FROM markets "
        "WHERE (status='open' OR status='' OR status IS NULL) "
        "ORDER BY close_time ASC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description="Show today's live events for Kalshi + Polymarket")
    parser.add_argument("--platform",     choices=["kalshi", "polymarket"], help="Show only one platform")
    parser.add_argument("--all",          action="store_true", help="Include tomorrow's markets too")
    parser.add_argument("--all-markets",  action="store_true", help="Show skipped/junk markets too")
    parser.add_argument("--min-conf",     type=float, default=75.0, help="Min confidence threshold (default 75)")
    parser.add_argument("--db",           default=None, help="Path to trading_system.db")
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
        print("ERROR: Cannot find trading_system.db. Pass --db /path/to/trading_system.db", file=sys.stderr)
        sys.exit(1)

    print(f"\n  DB: {db_path}")
    print(f"  Now: {_now_et().strftime('%A %B %d %Y %I:%M:%S %p ET')}")

    all_rows = _load_db(db_path)

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
        filtered = _filter(all_rows, plat)
        _print_table(plat, filtered, args.min_conf, args.all_markets)

    # Summary
    today_total = sum(
        1 for r in all_rows
        if _closes_today(r.get("close_time") or "")
        and (not args.platform or (r.get("platform") or "kalshi").lower() == args.platform)
    )
    print(f"  📊 Total markets closing today: {today_total}")
    print("  💡 Tip: python3 getlive_eventstoday.py --all-markets   → show junk/skip reasons")
    print("  💡 Tip: python3 getlive_eventstoday.py --all           → include tomorrow too")
    print("  💡 Tip: python3 getlive_eventstoday.py --platform kalshi\n")


if __name__ == "__main__":
    # Make sure we can import from the bot's src/ directory
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
