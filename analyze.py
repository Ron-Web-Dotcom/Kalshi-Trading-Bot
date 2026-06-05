#!/usr/bin/env python3
"""
Quick performance analysis — run anytime to see how the bot is doing.
Usage: python analyze.py
"""

import sqlite3
from datetime import datetime, timezone

import os
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "trading_system.db"))

def run():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    print("\n" + "="*60)
    print("  KALSHI BOT — PERFORMANCE SNAPSHOT")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("="*60)

    # ── 1. Overall win rate ───────────────────────────────────────
    r = con.execute("""
        SELECT COUNT(*) total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) losses,
               SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END) breakeven,
               ROUND(SUM(pnl), 2) total_pnl,
               ROUND(AVG(pnl), 2) avg_pnl
        FROM positions WHERE status='closed' AND pnl IS NOT NULL
    """).fetchone()
    total = r["total"] or 0
    wins  = r["wins"]  or 0
    losses= r["losses"]or 0
    wr    = (wins/total*100) if total > 0 else 0
    print(f"\n📊 OVERALL  ({total} closed trades)")
    print(f"   Win rate : {wr:.1f}%  ({wins}W / {losses}L)")
    print(f"   Total PnL: ${r['total_pnl'] or 0:+.2f}")
    print(f"   Avg PnL  : ${r['avg_pnl'] or 0:+.2f} per trade")

    if total < 10:
        print(f"   ⚠️  Sample too small — need 50+ trades to trust this")

    # ── 2. By platform ────────────────────────────────────────────
    print(f"\n📡 BY PLATFORM")
    rows = con.execute("""
        SELECT platform,
               COUNT(*) total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,
               ROUND(SUM(pnl), 2) pnl
        FROM positions WHERE status='closed' AND pnl IS NOT NULL
        GROUP BY platform
    """).fetchall()
    for row in rows:
        t = row["total"] or 0
        w = row["wins"]  or 0
        wr2 = (w/t*100) if t > 0 else 0
        plat = (row["platform"] or "kalshi").title()
        print(f"   {plat:<12} {t} trades  {wr2:.0f}% WR  ${row['pnl']:+.2f}")

    # ── 3. By confidence band ────────────────────────────────────
    print(f"\n🎯 BY AI CONFIDENCE BAND")
    rows = con.execute("""
        SELECT
            CASE
                WHEN tl.ai_confidence >= 80 THEN '80-100%'
                WHEN tl.ai_confidence >= 70 THEN '70-79%'
                WHEN tl.ai_confidence >= 60 THEN '60-69%'
                ELSE '<60%'
            END band,
            COUNT(*) total,
            SUM(CASE WHEN p.pnl > 0 THEN 1 ELSE 0 END) wins,
            ROUND(SUM(p.pnl), 2) pnl
        FROM positions p
        JOIN trade_logs tl ON tl.ticker = p.ticker
        WHERE p.status='closed' AND p.pnl IS NOT NULL AND tl.ai_confidence IS NOT NULL
        GROUP BY band ORDER BY band DESC
    """).fetchall()
    if rows:
        for row in rows:
            t = row["total"] or 0
            w = row["wins"]  or 0
            wr2 = (w/t*100) if t > 0 else 0
            print(f"   Conf {row['band']:<10} {t} trades  {wr2:.0f}% WR  ${row['pnl']:+.2f}")
    else:
        print("   No data yet")

    # ── 4. By entry price range ──────────────────────────────────
    print(f"\n💰 BY ENTRY PRICE RANGE")
    rows = con.execute("""
        SELECT
            CASE
                WHEN avg_price <= 30 THEN '≤30¢ (longshot)'
                WHEN avg_price <= 45 THEN '31-45¢'
                WHEN avg_price <= 55 THEN '46-55¢ (coin flip)'
                WHEN avg_price <= 70 THEN '56-70¢'
                ELSE '>70¢ (favourite)'
            END band,
            COUNT(*) total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,
            ROUND(SUM(pnl), 2) pnl
        FROM positions WHERE status='closed' AND pnl IS NOT NULL
        GROUP BY band
    """).fetchall()
    if rows:
        for row in rows:
            t = row["total"] or 0
            w = row["wins"]  or 0
            wr2 = (w/t*100) if t > 0 else 0
            print(f"   {row['band']:<22} {t} trades  {wr2:.0f}% WR  ${row['pnl']:+.2f}")
    else:
        print("   No data yet")

    # ── 5. By close reason ───────────────────────────────────────
    print(f"\n🔚 HOW TRADES CLOSED")
    rows = con.execute("""
        SELECT close_reason, COUNT(*) n, ROUND(SUM(pnl),2) pnl
        FROM positions WHERE status='closed' AND pnl IS NOT NULL
        GROUP BY close_reason ORDER BY n DESC
    """).fetchall()
    for row in rows:
        print(f"   {(row['close_reason'] or 'unknown'):<25} {row['n']} trades  ${row['pnl']:+.2f}")

    # ── 6. Open positions summary ────────────────────────────────
    open_rows = con.execute("""
        SELECT ticker, side, avg_price, pnl, platform, opened_at
        FROM positions WHERE status='open'
        ORDER BY pnl ASC
    """).fetchall()
    print(f"\n📂 OPEN POSITIONS ({len(open_rows)} total)")
    for row in open_rows:
        pnl_s = f"${row['pnl']:+.2f}" if row['pnl'] is not None else "$0.00"
        plat  = "🟣" if row["platform"] == "polymarket" else "🟦"
        ticker = (row["ticker"] or "")[:35]
        print(f"   {plat} {ticker:<35} {row['side'].upper()}@{row['avg_price']:.0f}¢  {pnl_s}")

    # ── 7. AI cost tracking ──────────────────────────────────────
    cost_row = con.execute("""
        SELECT ROUND(SUM(cost_usd), 4) total_cost, COUNT(*) calls
        FROM ai_decisions WHERE cost_usd IS NOT NULL
    """).fetchone()
    if cost_row and cost_row["calls"]:
        print(f"\n🤖 AI USAGE")
        print(f"   Total calls: {cost_row['calls']}")
        print(f"   Total cost : ${cost_row['total_cost'] or 0:.4f}")

    # ── 8. Readiness verdict ─────────────────────────────────────
    print(f"\n{'='*60}")
    print("  READINESS CHECK")
    print(f"{'='*60}")
    checks = [
        (total >= 50,   f"50+ closed trades ({total}/50)"),
        (wr >= 55,      f"Win rate ≥ 55% (currently {wr:.1f}%)"),
        (total > 0 and (r['total_pnl'] or 0) > 0, f"Positive total PnL (${r['total_pnl'] or 0:+.2f})"),
        (len(open_rows) > 0, f"Active positions ({len(open_rows)} open)"),
    ]
    all_pass = True
    for passed, label in checks:
        icon = "✅" if passed else "❌"
        print(f"   {icon} {label}")
        if not passed:
            all_pass = False

    if all_pass:
        print("\n  🟢 READY TO CONSIDER GOING LIVE")
    else:
        remaining = 50 - total
        print(f"\n  🔴 NOT READY — keep paper trading")
        if remaining > 0:
            print(f"     Need {remaining} more closed trades")
    print("="*60 + "\n")

    con.close()

if __name__ == "__main__":
    run()
