#!/usr/bin/env python3
"""
Weekly report — auto-runs every Monday at 6am ET via cron.
Saves to /root/trading-bot/reports/weekly_YYYY-MM-DD.txt

To install cron job (run once on the server):
  crontab -e
  Add: 0 6 * * 1 cd /root/trading-bot && /root/trading-bot/venv/bin/python weekly_report.py
"""

import sqlite3
import os
from datetime import datetime, timezone, timedelta

DB_PATH  = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "trading_system.db"))
OUT_DIR  = os.path.join(os.path.dirname(__file__), "reports")
os.makedirs(OUT_DIR, exist_ok=True)

WEEK_AGO = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()


def _bar(value: float, max_val: float, width: int = 20) -> str:
    filled = int((value / max_val) * width) if max_val > 0 else 0
    return "█" * filled + "░" * (width - filled)


def run():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    lines = []
    now   = datetime.now()

    def p(text=""):
        lines.append(text)

    p("=" * 65)
    p("  KALSHI BOT — WEEKLY REPORT")
    p(f"  Week ending : {now.strftime('%A, %B %d, %Y')}")
    p(f"  Generated   : {now.strftime('%Y-%m-%d %H:%M')}")
    p("=" * 65)

    # ── 1. This week vs all-time ──────────────────────────────────
    for label, where in [("THIS WEEK", f"AND closed_at >= '{WEEK_AGO}'"), ("ALL TIME", "")]:
        r = con.execute(f"""
            SELECT COUNT(*) total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,
                   SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) losses,
                   ROUND(SUM(pnl), 2) total_pnl,
                   ROUND(AVG(pnl), 2) avg_pnl,
                   ROUND(MAX(pnl), 2) best,
                   ROUND(MIN(pnl), 2) worst
            FROM positions WHERE status='closed' AND pnl IS NOT NULL {where}
        """).fetchone()
        total  = r["total"] or 0
        wins   = r["wins"]  or 0
        losses = r["losses"]or 0
        wr     = (wins / total * 100) if total > 0 else 0
        p(f"\n📊 {label}  ({total} closed trades)")
        p(f"   Win rate  : {wr:.1f}%  ({wins}W / {losses}L)  {_bar(wr, 100)}")
        p(f"   Total PnL : ${r['total_pnl'] or 0:+.2f}")
        p(f"   Avg/trade : ${r['avg_pnl']   or 0:+.2f}")
        p(f"   Best win  : ${r['best']       or 0:+.2f}    Worst loss: ${r['worst'] or 0:+.2f}")
        if total < 10:
            p(f"   ⚠️  Sample too small — need 50+ trades to trust this")

    # ── 2. This week by platform ──────────────────────────────────
    p(f"\n📡 THIS WEEK BY PLATFORM")
    rows = con.execute(f"""
        SELECT platform,
               COUNT(*) total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,
               ROUND(SUM(pnl), 2) pnl
        FROM positions WHERE status='closed' AND pnl IS NOT NULL
        AND closed_at >= '{WEEK_AGO}'
        GROUP BY platform
    """).fetchall()
    if rows:
        for row in rows:
            t   = row["total"] or 0
            w   = row["wins"]  or 0
            wr2 = (w / t * 100) if t > 0 else 0
            plat = (row["platform"] or "kalshi").title()
            p(f"   {plat:<12} {t} trades  {wr2:.0f}% WR  ${row['pnl']:+.2f}")
    else:
        p("   No trades this week")

    # ── 3. This week by confidence band ──────────────────────────
    p(f"\n🎯 THIS WEEK BY AI CONFIDENCE BAND")
    rows = con.execute(f"""
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
        WHERE p.status='closed' AND p.pnl IS NOT NULL
        AND p.closed_at >= '{WEEK_AGO}'
        AND tl.ai_confidence IS NOT NULL
        GROUP BY band ORDER BY band DESC
    """).fetchall()
    if rows:
        for row in rows:
            t   = row["total"] or 0
            w   = row["wins"]  or 0
            wr2 = (w / t * 100) if t > 0 else 0
            p(f"   Conf {row['band']:<10} {t} trades  {wr2:.0f}% WR  ${row['pnl']:+.2f}")
    else:
        p("   No data this week")

    # ── 4. AI cost this week ──────────────────────────────────────
    p(f"\n🤖 AI USAGE THIS WEEK")
    cost = con.execute(f"""
        SELECT ROUND(SUM(cost_usd), 4) total_cost,
               COUNT(*) calls,
               ROUND(AVG(cost_usd), 6) avg_cost
        FROM ai_decisions
        WHERE cost_usd IS NOT NULL AND created_at >= '{WEEK_AGO}'
    """).fetchone()
    cost_all = con.execute("""
        SELECT ROUND(SUM(cost_usd), 4) total_cost, COUNT(*) calls
        FROM ai_decisions WHERE cost_usd IS NOT NULL
    """).fetchone()
    if cost and cost["calls"]:
        weekly_cost = cost["total_cost"] or 0
        monthly_est = weekly_cost * 4.33
        p(f"   Calls this week : {cost['calls']}")
        p(f"   Cost this week  : ${weekly_cost:.4f}")
        p(f"   Avg per call    : ${cost['avg_cost'] or 0:.6f}")
        p(f"   Monthly estimate: ${monthly_est:.2f}")
        p(f"   All-time total  : ${cost_all['total_cost'] or 0:.4f} ({cost_all['calls']} calls)")
    else:
        p("   No AI cost data tracked yet")
        p("   (ai_decisions table may not have cost_usd column)")

    # ── 5. Top wins this week ─────────────────────────────────────
    p(f"\n🏆 TOP 5 WINS THIS WEEK")
    rows = con.execute(f"""
        SELECT ticker, platform, pnl, avg_price, closed_at
        FROM positions WHERE status='closed' AND pnl > 0
        AND closed_at >= '{WEEK_AGO}'
        ORDER BY pnl DESC LIMIT 5
    """).fetchall()
    if rows:
        for i, row in enumerate(rows, 1):
            plat = "🟣" if row["platform"] == "polymarket" else "🟦"
            ticker = (row["ticker"] or "")[:40]
            p(f"   {i}. {plat} {ticker:<40} +${row['pnl']:.2f}")
    else:
        p("   No wins this week")

    # ── 6. Top losses this week ───────────────────────────────────
    p(f"\n💸 TOP 5 LOSSES THIS WEEK")
    rows = con.execute(f"""
        SELECT ticker, platform, pnl, avg_price, closed_at
        FROM positions WHERE status='closed' AND pnl < 0
        AND closed_at >= '{WEEK_AGO}'
        ORDER BY pnl ASC LIMIT 5
    """).fetchall()
    if rows:
        for i, row in enumerate(rows, 1):
            plat = "🟣" if row["platform"] == "polymarket" else "🟦"
            ticker = (row["ticker"] or "")[:40]
            p(f"   {i}. {plat} {ticker:<40} ${row['pnl']:.2f}")
    else:
        p("   No losses this week")

    # ── 7. Open positions right now ───────────────────────────────
    open_rows = con.execute("""
        SELECT ticker, side, avg_price, pnl, platform, opened_at
        FROM positions WHERE status='open'
        ORDER BY opened_at DESC
    """).fetchall()
    p(f"\n📂 OPEN POSITIONS RIGHT NOW ({len(open_rows)} total)")
    if open_rows:
        for row in open_rows:
            pnl_s  = f"${row['pnl']:+.2f}" if row['pnl'] is not None else "$0.00"
            plat   = "🟣" if row["platform"] == "polymarket" else "🟦"
            ticker = (row["ticker"] or "")[:38]
            p(f"   {plat} {ticker:<38} {row['side'].upper()}@{row['avg_price']:.0f}¢  {pnl_s}")
    else:
        p("   No open positions")

    # ── 8. Go-live readiness ──────────────────────────────────────
    r_all = con.execute("""
        SELECT COUNT(*) total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,
               ROUND(SUM(pnl), 2) total_pnl
        FROM positions WHERE status='closed' AND pnl IS NOT NULL
    """).fetchone()
    total_all = r_all["total"] or 0
    wins_all  = r_all["wins"]  or 0
    wr_all    = (wins_all / total_all * 100) if total_all > 0 else 0

    p(f"\n{'=' * 65}")
    p("  GO-LIVE READINESS CHECK")
    p(f"{'=' * 65}")
    checks = [
        (total_all >= 50,              f"50+ closed trades ({total_all}/50)"),
        (wr_all >= 55,                 f"Win rate ≥ 55% (currently {wr_all:.1f}%)"),
        ((r_all['total_pnl'] or 0) > 0, f"Positive total PnL (${r_all['total_pnl'] or 0:+.2f})"),
        (len(open_rows) > 0,           f"Active positions ({len(open_rows)} open)"),
    ]
    all_pass = True
    for passed, label in checks:
        icon = "✅" if passed else "❌"
        p(f"   {icon} {label}")
        if not passed:
            all_pass = False

    if all_pass:
        p("\n  🟢 READY TO CONSIDER GOING LIVE")
    else:
        remaining = max(0, 50 - total_all)
        p(f"\n  🔴 NOT READY — keep paper trading")
        if remaining > 0:
            p(f"     Need {remaining} more closed trades")

    p("=" * 65)
    p(f"  Next check-in: {(now + timedelta(days=7)).strftime('%A, %B %d, %Y')}")
    p("=" * 65 + "\n")

    con.close()

    # Save to file
    report = "\n".join(lines)
    filename = os.path.join(OUT_DIR, f"weekly_{now.strftime('%Y-%m-%d')}.txt")
    with open(filename, "w") as f:
        f.write(report)

    print(report)
    print(f"\n✅ Report saved to: {filename}")


if __name__ == "__main__":
    run()
