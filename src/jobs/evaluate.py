"""Job: evaluate performance and update metrics."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("trading.jobs.evaluate")


_last_discord_trade_bucket: int = -1


async def run_evaluation(db=None, scaler=None) -> None:
    """Compute and store a performance snapshot, log a clean summary table."""
    global _last_discord_trade_bucket
    from src.utils.database import DatabaseManager
    from src.alerts.discord import DiscordAlerter
    from src.risk.scaling import AutoScaler

    if db is None:
        db = DatabaseManager()
        await db.initialize()

    try:
        from src.config.settings import settings
        paper_flag = 0 if settings.trading.live_trading_enabled else 1
        mode_label = "LIVE" if settings.trading.live_trading_enabled else "PAPER"

        stats = await db.fetchone(f"""
            SELECT
                COUNT(*)                                           AS total_trades,
                SUM(CASE WHEN pnl > 0  THEN 1 ELSE 0 END)        AS winning_trades,
                SUM(CASE WHEN pnl < 0  THEN 1 ELSE 0 END)        AS losing_trades,
                SUM(CASE WHEN pnl IS NULL THEN 1 ELSE 0 END)      AS open_trades,
                SUM(COALESCE(pnl, 0))                             AS total_pnl,
                AVG(ai_confidence)                                    AS avg_confidence
            FROM trade_logs WHERE paper_trade={paper_flag}
        """)
        if not stats or not stats.get("total_trades"):
            logger.info("[EVAL] No %s trades yet — nothing to evaluate", mode_label)
            return

        total     = stats["total_trades"]      or 0
        wins      = stats["winning_trades"]     or 0
        losses    = stats["losing_trades"]      or 0
        open_pos  = stats["open_trades"]        or 0
        total_pnl = stats["total_pnl"]          or 0.0
        avg_conf  = stats["avg_confidence"]     or 0.0
        win_rate = (wins / (total - open_pos) * 100) if (total - open_pos) > 0 else 0.0

        # Last 5 completed trades
        last5 = await db.fetchall(f"""
            SELECT ticker, action, side, price, contracts, total_cost, pnl,
                   signal_source, ai_confidence, executed_at
            FROM trade_logs
            WHERE paper_trade={paper_flag}
            ORDER BY executed_at DESC LIMIT 5
        """)

        scaler      = scaler if scaler is not None else AutoScaler()
        scale_factor = scaler.update(total_pnl)

        now  = datetime.now(timezone.utc).isoformat()
        today = now[:10]  # YYYY-MM-DD

        await db.insert("performance_metrics", {
            "total_trades":         total,
            "winning_trades":       wins,
            "losing_trades":        losses,
            "total_pnl":            total_pnl,
            "win_rate":             win_rate,
            "current_scale_factor": scaler.scale_factor,
            "recorded_at":          now,
        })

        # Upsert today's daily_stats
        daily = await db.fetchone(
            "SELECT trades, pnl, ai_cost FROM daily_stats WHERE date=?", (today,)
        )
        ai_cost = await db.get_daily_ai_cost()
        if daily:
            await db.execute(
                "UPDATE daily_stats SET trades=?, pnl=?, ai_cost=? WHERE date=?",
                (total, total_pnl, ai_cost, today)
            )
        else:
            await db.execute(
                "INSERT INTO daily_stats (date, trades, pnl, ai_cost) VALUES (?,?,?,?)",
                (today, total, total_pnl, ai_cost)
            )

        # ── Console summary ──────────────────────────────────────────────────
        pnl_sign = "+" if total_pnl >= 0 else ""
        logger.info("╔══════════════════════════════════════════════╗")
        logger.info("║     PERFORMANCE SNAPSHOT (%-5s)              ║", mode_label)
        logger.info("╠══════════════════════════════════════════════╣")
        logger.info("║  Total trades  : %-5d (open: %-3d)           ║", total, open_pos)
        logger.info("║  Win / Loss    : %-3d / %-3d                  ║", wins, losses)
        logger.info("║  Win rate      : %.1f%%                        ║", win_rate)
        logger.info("║  Total PnL     : %s$%.2f                      ║", pnl_sign, abs(total_pnl))
        logger.info("║  Avg confidence: %.0f%%                         ║", avg_conf)
        logger.info("║  Scale factor  : %.2fx (base×factor)           ║", scaler.scale_factor)
        logger.info("╠══════════════════════════════════════════════╣")
        if last5:
            logger.info("║  LAST 5 TRADES                               ║")
            logger.info("║  %-12s %-6s %-4s %5s  %6s  %-9s  ║",
                        "TICKER", "ACTION", "SIDE", "PRICE", "PnL", "SOURCE")
            for t in last5:
                pnl_str = f"${t['pnl']:+.2f}" if t["pnl"] is not None else "open"
                src     = (t.get("signal_source") or "")[:9]
                logger.info("║  %-12s %-6s %-4s %4.0f¢  %6s  %-9s  ║",
                            (t["ticker"] or "")[:12],
                            t["action"], t["side"],
                            t["price"], pnl_str, src)
        logger.info("╚══════════════════════════════════════════════╝")

        # Discord summary only when trade count has increased by ≥50 since last send
        if total > 0 and total // 50 > _last_discord_trade_bucket:
            _last_discord_trade_bucket = total // 50
            discord = DiscordAlerter()
            await discord.pnl_update(total_pnl, win_rate, total, scaler.scale_factor)

    except Exception as e:
        logger.error("Evaluation error: %s", e, exc_info=True)
