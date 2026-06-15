#!/usr/bin/env python3
"""
Cleanup: close all open paper positions that don't belong.

Closes positions if ANY of these are true:
  1. close_time is beyond 7 days from today (not a near-term event)
  2. Title matches a known junk/long-term phrase
  3. Opened more than 7 days ago

Usage: python cleanup_old_positions.py
"""

import sqlite3
import os
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "trading_system.db"))

JUNK_PHRASES = [
    # Long-term futures / novelty
    "before gta", "gta vi", "gta 6",
    "jesus christ", "second coming", "rapture",
    "before 2027", "before 2028", "before 2029", "before 2030",
    "before 203", "before 204",
    "win the 2028", "win the 2032", "2028 us presidential",
    "gavin newsom", "2028 democratic", "2028 president",
    "waymo launch", "waymo nashville",
    "bernie endorse", "endorse dan osborn",
    "bitcoin hit $150k", "hit $150k", "hit $1m", "hit $500k",
    "oprah", "lebron james president", "taylor swift president",
    "elon musk president", "win the world cup 2026",
    "uzbekistan win", "world cup 2026 winner",
    "nba finals 2026 winner", "nba champion", "stanley cup 2026 winner",
    "win the nba", "win the stanley cup",
    "megaeth", "airdrop by",
    "korea republic vs. czechia", "korea republic vs czechia",
    "russia vs. trinidad", "russia vs trinidad",
    "victor wembanyama", "wembanyama rebounds",
    "gen.g esports", "lck 2026",
    "keir starmer", "labour par",
    "ivan cepeda", "colombian presiden",
    "democratic union of hungarians",
    # Price targets way out in the future
    "by december 31", "by december 2026", "by end of 2026",
    "by january 2027", "by 2027",
    # Political futures beyond this year
    "win the 2026 nba", "2026 nba finals",
]

now_utc     = datetime.now(timezone.utc)
cutoff_open = (now_utc - timedelta(days=7)).isoformat()   # opened > 7 days ago
cutoff_close = (now_utc + timedelta(days=3)).isoformat()  # closes > 3 days from now

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row

rows = con.execute("""
    SELECT p.id, p.ticker, p.title, p.platform, p.opened_at, p.avg_price, p.pnl,
           m.close_time
    FROM positions p
    LEFT JOIN markets m ON m.ticker = p.ticker
    WHERE p.status='open'
""").fetchall()

print(f"\nToday: {now_utc.strftime('%B %d, %Y')}")
print(f"Found {len(rows)} open positions")
print("=" * 65)

closed_far    = 0
closed_junk   = 0
closed_old    = 0
kept          = 0
now_str       = now_utc.isoformat()

for row in rows:
    title     = (row["title"] or row["ticker"] or "").lower()
    opened_at = row["opened_at"] or ""
    close_time = row["close_time"] or ""
    reason    = None

    # 1. Check if close_time is beyond 3 days
    if close_time and not reason:
        try:
            close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
            if close_dt > now_utc + timedelta(days=3):
                reason = f"closes too far out ({close_dt.strftime('%b %d, %Y')})"
                closed_far += 1
        except Exception:
            pass

    # 2. Check junk title
    if not reason:
        for phrase in JUNK_PHRASES:
            if phrase in title:
                reason = f"junk market: '{phrase}'"
                closed_junk += 1
                break

    # 3. Check if opened more than 7 days ago
    if not reason and opened_at:
        try:
            opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=timezone.utc)
            if opened_dt < now_utc - timedelta(days=7):
                reason = "opened more than 7 days ago"
                closed_old += 1
        except Exception:
            pass

    if reason:
        con.execute("""
            UPDATE positions
            SET status='closed', close_reason=?, closed_at=?, pnl=COALESCE(pnl, 0)
            WHERE id=?
        """, (f"cleanup: {reason}", now_str, row["id"]))
        label = (row["title"] or row["ticker"] or "?")[:55]
        print(f"  CLOSED  {label}")
        print(f"          → {reason}")
    else:
        kept += 1
        label = (row["title"] or row["ticker"] or "?")[:55]
        print(f"  KEPT    {label}")

con.commit()
con.close()

print("=" * 65)
print(f"\n✅ Closed {closed_far} positions resolving beyond 7 days")
print(f"✅ Closed {closed_junk} junk/long-term market positions")
print(f"✅ Closed {closed_old} positions opened more than 7 days ago")
print(f"   Kept   {kept} valid near-term positions")
print(f"\nBot now has room for today's live events.\n")
