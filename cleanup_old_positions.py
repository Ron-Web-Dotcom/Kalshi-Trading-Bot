#!/usr/bin/env python3
"""
One-time cleanup: close all open paper positions opened more than 7 days ago.
Also closes positions with junk titles (long-term futures, novelty markets).

Usage: python cleanup_old_positions.py
"""

import sqlite3
import os
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "trading_system.db"))

JUNK_PHRASES = [
    "before gta", "gta vi", "gta 6",
    "jesus christ", "second coming",
    "before 2027", "before 2028", "before 2029", "before 2030",
    "win the 2028", "win the 2032", "2028 us presidential",
    "gavin newsom", "2028 democratic",
    "waymo launch", "waymo nashville",
    "bernie endorse", "endorse dan osborn",
    "bitcoin hit $150k", "bitcoin hit $1", "bitcoin hit $500",
    "hit $150k", "hit $1m", "hit $500k",
    "oprah", "lebron james president", "taylor swift president",
    "elon musk president", "win the world cup 2026",
    "uzbekistan win",
]

cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row

# Find all open positions
rows = con.execute("""
    SELECT id, ticker, title, platform, opened_at, avg_price, pnl
    FROM positions WHERE status='open'
""").fetchall()

print(f"\nFound {len(rows)} open positions total")
print("=" * 60)

closed_old   = 0
closed_junk  = 0
kept         = 0

now_str = datetime.now(timezone.utc).isoformat()

for row in rows:
    title      = (row["title"] or row["ticker"] or "").lower()
    opened_at  = row["opened_at"] or ""
    reason     = None

    # Check if older than 7 days
    try:
        opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        if opened_dt.tzinfo is None:
            opened_dt = opened_dt.replace(tzinfo=timezone.utc)
        if opened_dt < datetime.fromisoformat(cutoff):
            reason = "older than 7 days"
    except Exception:
        pass

    # Check if junk title
    if not reason:
        for phrase in JUNK_PHRASES:
            if phrase in title:
                reason = f"junk market: '{phrase}'"
                break

    if reason:
        con.execute("""
            UPDATE positions
            SET status='closed', close_reason=?, closed_at=?, pnl=COALESCE(pnl, 0)
            WHERE id=?
        """, (f"cleanup: {reason}", now_str, row["id"]))
        label = (row["title"] or row["ticker"] or "?")[:55]
        print(f"  CLOSED [{row['platform'] or 'kalshi'}] {label}")
        print(f"         reason: {reason}")
        if "junk" in reason:
            closed_junk += 1
        else:
            closed_old += 1
    else:
        kept += 1

con.commit()
con.close()

print("=" * 60)
print(f"\n✅ Closed {closed_old} old positions (>7 days)")
print(f"✅ Closed {closed_junk} junk market positions")
print(f"   Kept   {kept} active positions")
print(f"\nBot will now have room for fresh trades on new markets.\n")
