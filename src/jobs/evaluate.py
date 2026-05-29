"""Job: evaluate performance and update metrics."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("trading.jobs.evaluate")


async def run_evaluation(db=None) -> None:
    """Compute and store performance snapshot."""
    from src.utils.database import DatabaseManager
    from src.alerts.discord import DiscordAlerter
    from src.risk.scaling import AutoScaler

    if db is None:
        db = DatabaseManager()
        await db.initialize()

    try:
        stats = await db.fetchone("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                SUM(COALESCE(pnl, 0)) as total_pnl
            FROM trade_logs WHERE paper_trade=1
        """)
        if not stats or not stats.get("total_trades"):
            return

        total = stats["total_trades"] or 0
        wins = stats["winning_trades"] or 0
        win_rate = (wins / total * 100) if total > 0 else 0.0
        total_pnl = stats["total_pnl"] or 0.0

        scaler = AutoScaler()
        scale_factor = scaler.update(total_pnl)

        now = datetime.now(timezone.utc).isoformat()
        await db.insert("performance_metrics", {
            "total_trades": total,
            "winning_trades": wins,
            "losing_trades": stats.get("losing_trades") or 0,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "current_scale_factor": scaler.scale_factor,
            "recorded_at": now,
        })

        logger.info(
            f"[EVAL] Trades={total} | WinRate={win_rate:.1f}% | "
            f"PnL=${total_pnl:+.2f} | Scale={scaler.scale_factor:.2f}x"
        )

        # Discord summary every 10 trades
        if total > 0 and total % 10 == 0:
            discord = DiscordAlerter()
            await discord.pnl_update(total_pnl, win_rate, total, scaler.scale_factor)

    except Exception as e:
        logger.error(f"Evaluation error: {e}")
