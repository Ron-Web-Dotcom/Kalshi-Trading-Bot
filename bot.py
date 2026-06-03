#!/usr/bin/env python3
"""
Kalshi AI Trading Bot — main entry point.

Usage:
    python bot.py                    # Paper trading (safe default)
    python bot.py --live             # Live trading (requires LIVE_TRADING_ENABLED=true in .env)
    python bot.py --once             # One cycle then exit (good for testing)
    python bot.py --log-level DEBUG  # Verbose output
"""

import asyncio
import argparse
import logging
import signal
import sys
from datetime import datetime, timezone

from src.utils.logging_setup import setup_logging, get_trading_logger
from src.utils.database import DatabaseManager
from src.jobs.ingest import run_ingestion
from src.jobs.trade import run_trading_job
from src.jobs.track import run_tracking
from src.config.settings import settings

logger = get_trading_logger("main")

# Cycle intervals (seconds)
INGEST_INTERVAL  = 300   # refresh market data every 5 min
TRACK_INTERVAL   = 120   # check position PnL every 2 min
EVAL_INTERVAL    = 300   # print performance snapshot every 5 min
TRADE_INTERVAL   = 60    # run trading cycle every 60 s
HEARTBEAT_INTERVAL = 3600  # send hourly heartbeat every 60 min


class TradingBot:
    def __init__(self, live_mode: bool = False):
        # Safety: live mode requires the env var AND the CLI flag
        if live_mode and not settings.trading.live_trading_enabled:
            logger.error(
                "Cannot start LIVE: LIVE_TRADING_ENABLED=false in .env. "
                "Set it to true only after reviewing paper trade results."
            )
            sys.exit(1)

        settings.trading.live_trading_enabled = live_mode
        settings.trading.paper_trading_mode   = not live_mode

        self.live_mode  = live_mode
        self.db         = DatabaseManager()
        self._shutdown  = asyncio.Event()
        self._cycle     = 0

        # Singletons — share state across cycles so cooldowns, scaling, and
        # arb signal dedup persist without restarting (fixes A10/A11/A12)
        from src.risk.manager import RiskManager
        from src.risk.scaling import AutoScaler
        from src.strategy.arbitrage import ArbitrageDetector
        self.risk    = RiskManager(db=self.db)
        self.scaler  = AutoScaler()
        self.arb_det = ArbitrageDetector()

    def _print_startup_banner(self):
        mode  = "LIVE TRADING  ⚠️  REAL MONEY" if self.live_mode else "PAPER TRADING  ✅  No real money"
        lines = [
            "╔══════════════════════════════════════════════════╗",
            f"║  KALSHI AI TRADING BOT                           ║",
            f"║  Mode      : {mode:<36}║",
            f"║  AI model  : {settings.ai.model:<36}║",
            f"║  Base size : ${settings.trading.base_trade_size_dollars:<5.0f}  "
            f"Max: ${settings.trading.max_trade_size_dollars:<5.0f}  "
            f"Kelly: {settings.trading.kelly_fraction:.0%}     ║",
            f"║  Min conf  : {settings.trading.min_ai_confidence:.0f}%   "
            f"Arb threshold: {settings.trading.arbitrage_threshold_pct:.0f}%              ║",
            f"║  Daily loss cap : {settings.trading.max_daily_loss_pct:.0f}%   "
            f"Cooldown: {settings.trading.cooldown_between_trades_seconds}s              ║",
            "╚══════════════════════════════════════════════════╝",
        ]
        for line in lines:
            logger.info(line)

    async def startup(self):
        # Run health check before startup banner
        health = {}
        try:
            from src.utils.health_check import HealthChecker
            checker = HealthChecker()
            health = await checker.run_all()
        except Exception as _he:
            logger.warning("Health check failed: %s", _he)

        self._print_startup_banner()
        logger.info("Initializing database...")
        await self.db.initialize()
        logger.info("Running initial market ingestion...")
        try:
            count = await run_ingestion(self.db)
            logger.info("Initial ingestion complete: %d markets cached", count)
        except Exception as e:
            logger.warning("Initial ingestion failed (will retry): %s", e)

        # Discord startup alert
        try:
            from src.alerts.discord import DiscordAlerter
            discord = DiscordAlerter()
            balance = None
            if self.live_mode:
                try:
                    from src.clients.kalshi_client import KalshiClient
                    k = KalshiClient()
                    bal = await k.get_balance()
                    balance = (bal.get("balance") or 0) / 100
                    await k.close()
                except Exception:
                    pass
            await discord.startup_banner(
                mode="LIVE" if self.live_mode else "PAPER",
                balance=balance,
                health_results=health,
            )
        except Exception:
            pass

    async def run_cycle(self):
        self._cycle += 1
        logger.info(
            "┌── Trading Cycle #%d ─────────────────────────────────────",
            self._cycle,
        )
        results = await run_trading_job(
            db=self.db, risk=self.risk, scaler=self.scaler, arb_det=self.arb_det
        )
        logger.info(
            "└── Cycle #%d done: %d trade(s) (arb=%d ai=%d skip=%d) $%.2f capital",
            self._cycle,
            results.total_positions,
            results.arb_trades,
            results.ai_trades,
            results.skipped,
            results.total_capital_used,
        )

    async def run_loop(self):
        await self.startup()

        async def ingest_loop():
            while not self._shutdown.is_set():
                await asyncio.sleep(INGEST_INTERVAL)
                try:
                    count = await run_ingestion(self.db)
                    logger.info("Market refresh: %d markets", count)
                except Exception as e:
                    logger.error("Ingest error: %s", e)

        async def track_loop():
            await asyncio.sleep(TRACK_INTERVAL)  # let first cycle run first
            while not self._shutdown.is_set():
                try:
                    await run_tracking(self.db)
                except Exception as e:
                    logger.error("Track error: %s", e)
                await asyncio.sleep(TRACK_INTERVAL)

        async def trade_loop():
            while not self._shutdown.is_set():
                try:
                    await self.run_cycle()
                except Exception as e:
                    logger.error("Trade cycle error: %s", e)
                await asyncio.sleep(TRADE_INTERVAL)

        async def hourly_heartbeat_loop():
            """Send an hourly heartbeat to Discord with scan stats and top candidates."""
            from src.alerts.discord import DiscordAlerter
            # Fire first heartbeat quickly after startup so user sees immediate status
            await asyncio.sleep(90)
            while not self._shutdown.is_set():
                try:
                    discord = DiscordAlerter()
                    today = datetime.now(timezone.utc).date().isoformat()

                    # Total markets in DB
                    mkt_row = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM markets"
                    )
                    markets_total = (mkt_row or {}).get("n", 0)

                    # Kalshi vs Polymarket split
                    kal_row = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM markets WHERE platform='kalshi' OR platform IS NULL"
                    )
                    poly_row = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM markets WHERE platform='polymarket'"
                    )
                    kalshi_count = (kal_row or {}).get("n", 0)
                    poly_count   = (poly_row or {}).get("n", 0)

                    # Open positions
                    open_row = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM positions WHERE status='open'"
                    )
                    open_n = (open_row or {}).get("n", 0)

                    # Today's PnL (paper or live depending on mode)
                    _paper_flag = 0 if settings.trading.live_trading_enabled else 1
                    pnl_row = await self.db.fetchone(
                        "SELECT COALESCE(SUM(pnl),0) as pnl FROM trade_logs "
                        "WHERE executed_at >= ? AND pnl IS NOT NULL AND paper_trade=?",
                        (today + "T00:00:00", _paper_flag)
                    )
                    paper_pnl = (pnl_row or {}).get("pnl", 0.0)

                    # All-time win rate — the bot's track record
                    wl_row = await self.db.fetchone(
                        "SELECT "
                        "  COUNT(*) as total, "
                        "  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                        "  SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses, "
                        "  COALESCE(SUM(pnl), 0) as total_pnl "
                        "FROM positions WHERE status='closed' AND pnl IS NOT NULL"
                    )
                    wl = wl_row or {}
                    total_closed = wl.get("total", 0) or 0
                    total_wins   = wl.get("wins",  0) or 0
                    total_losses = wl.get("losses",0) or 0
                    total_pnl    = wl.get("total_pnl", 0.0) or 0.0
                    win_rate     = (total_wins / total_closed * 100) if total_closed > 0 else 0.0

                    # Top 3 candidates by volume (yes_ask between 5 and 95)
                    candidates_rows = await self.db.fetchall(
                        "SELECT ticker, title, yes_ask, no_ask, volume, platform "
                        "FROM markets "
                        "WHERE yes_ask > 5 AND yes_ask < 95 "
                        "ORDER BY volume DESC LIMIT 3"
                    )
                    top_candidates = [
                        {
                            "ticker":   r["ticker"],
                            "title":    r.get("title", ""),
                            "yes_ask":  r.get("yes_ask", 0),
                            "no_ask":   r.get("no_ask",  0),
                            "volume":   r.get("volume",  0),
                            "platform": r.get("platform", "kalshi"),
                        }
                        for r in (candidates_rows or [])
                    ]

                    # Today's closed trades with outcomes
                    closed_rows = await self.db.fetchall(
                        "SELECT ticker, side, pnl, close_reason FROM positions "
                        "WHERE status='closed' AND closed_at >= ? ORDER BY closed_at DESC LIMIT 10",
                        (today + "T00:00:00",)
                    )
                    closed_trades = [dict(r) for r in (closed_rows or [])]

                    from src.utils.daily_stats import stats as daily_stats
                    await discord.hourly_heartbeat(
                        markets_scanned=markets_total,
                        kalshi_count=kalshi_count,
                        poly_count=poly_count,
                        top_candidates=top_candidates,
                        open_positions=open_n,
                        paper_pnl=paper_pnl,
                        paper=not settings.trading.live_trading_enabled,
                        closed_trades=closed_trades,
                        win_rate=win_rate,
                        total_wins=total_wins,
                        total_losses=total_losses,
                        total_pnl=total_pnl,
                        total_closed=total_closed,
                        best_pick=daily_stats.best_pick(),
                    )
                except Exception as e:
                    logger.error("Hourly heartbeat error: %s", e)

                # ── Near-miss digest (separate message) ───────────────────
                try:
                    await discord.near_miss_digest(
                        paper=not settings.trading.live_trading_enabled
                    )
                except Exception:
                    pass

                # ── Active position monitor (separate message) ────────────
                try:
                    open_pos = await self.db.fetchall(
                        "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
                    )
                    if open_pos:
                        await discord.position_monitor(
                            positions=open_pos,
                            paper=not settings.trading.live_trading_enabled,
                        )
                except Exception:
                    pass

                await asyncio.sleep(HEARTBEAT_INTERVAL)

        async def daytime_summary_loop():
            """Post morning (6 AM UTC) and afternoon (12 PM UTC) position digests."""
            from src.alerts.discord import DiscordAlerter
            from datetime import timedelta

            last_summary_at = datetime.now(timezone.utc).isoformat()

            while not self._shutdown.is_set():
                now = datetime.now(timezone.utc)
                targets = [now.replace(hour=6,  minute=0, second=0, microsecond=0),
                           now.replace(hour=12, minute=0, second=0, microsecond=0)]
                upcoming = [t if t > now else t + timedelta(days=1) for t in targets]
                next_time = min(upcoming)
                period = "Morning" if next_time.hour == 6 else "Afternoon"
                secs_until = (next_time - now).total_seconds()
                await asyncio.sleep(secs_until)
                if self._shutdown.is_set():
                    break

                try:
                    discord     = DiscordAlerter()
                    today       = datetime.now(timezone.utc).date().isoformat()
                    _paper_flag = 0 if settings.trading.live_trading_enabled else 1

                    open_pos = await self.db.fetchall(
                        "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
                    )
                    open_pos = [dict(r) for r in (open_pos or [])]

                    new_pos = await self.db.fetchall(
                        "SELECT * FROM positions WHERE status='open' AND opened_at >= ? ORDER BY opened_at DESC",
                        (last_summary_at,)
                    )
                    new_pos = [dict(r) for r in (new_pos or [])]

                    pnl_row = await self.db.fetchone(
                        "SELECT COALESCE(SUM(pnl),0) as pnl FROM trade_logs "
                        "WHERE executed_at >= ? AND pnl IS NOT NULL AND paper_trade=?",
                        (today + "T00:00:00", _paper_flag)
                    )
                    today_pnl = (pnl_row or {}).get("pnl", 0.0)

                    kal_row  = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM markets WHERE platform='kalshi' OR platform IS NULL"
                    )
                    poly_row = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM markets WHERE platform='polymarket'"
                    )
                    kalshi_count = (kal_row  or {}).get("n", 0)
                    poly_count   = (poly_row or {}).get("n", 0)

                    wl = await self.db.fetchone(
                        "SELECT COUNT(*) as total, "
                        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                        "SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses "
                        "FROM positions WHERE status='closed' AND pnl IS NOT NULL"
                    ) or {}
                    total_closed = wl.get("total", 0) or 0
                    total_wins   = wl.get("wins",  0) or 0
                    total_losses = wl.get("losses",0) or 0
                    win_rate     = (total_wins / total_closed * 100) if total_closed > 0 else 0.0

                    await discord.daytime_summary(
                        period=period,
                        open_positions=open_pos,
                        new_positions=new_pos,
                        today_pnl=today_pnl,
                        kalshi_count=kalshi_count,
                        poly_count=poly_count,
                        win_rate=win_rate,
                        total_wins=total_wins,
                        total_losses=total_losses,
                        total_closed=total_closed,
                        paper=not settings.trading.live_trading_enabled,
                    )
                    last_summary_at = datetime.now(timezone.utc).isoformat()
                except Exception as e:
                    logger.error("Daytime summary error: %s", e)

        async def daily_summary_loop():
            """Post midnight daily report to Discord, then reset daily stats."""
            from src.alerts.discord import DiscordAlerter
            from src.utils.daily_stats import stats as daily_stats

            while not self._shutdown.is_set():
                # Sleep until next midnight UTC
                now = datetime.now(timezone.utc)
                midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
                from datetime import timedelta
                next_midnight = midnight + timedelta(days=1)
                secs_until = (next_midnight - now).total_seconds()
                await asyncio.sleep(secs_until)

                try:
                    discord     = DiscordAlerter()
                    today       = now.date().isoformat()
                    snap        = daily_stats.snapshot()
                    paper       = not settings.trading.live_trading_enabled
                    _paper_flag = 0 if settings.trading.live_trading_enabled else 1

                    # Win/loss record
                    wl = await self.db.fetchone(
                        "SELECT COUNT(*) as total, "
                        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                        "SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses, "
                        "COALESCE(SUM(pnl),0) as total_pnl "
                        "FROM positions WHERE status='closed' AND pnl IS NOT NULL"
                    ) or {}
                    today_pnl_row = await self.db.fetchone(
                        "SELECT COALESCE(SUM(pnl),0) as pnl FROM trade_logs "
                        "WHERE executed_at >= ? AND pnl IS NOT NULL AND paper_trade=?",
                        (today + "T00:00:00", _paper_flag)
                    ) or {}
                    closed_today = await self.db.fetchall(
                        "SELECT ticker, side, pnl, close_reason FROM positions "
                        "WHERE status='closed' AND closed_at >= ? ORDER BY closed_at DESC",
                        (today + "T00:00:00",)
                    ) or []
                    open_row = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM positions WHERE status='open'"
                    ) or {}

                    await discord.midnight_daily_summary(
                        date=today,
                        snap=snap,
                        wins=wl.get("wins", 0) or 0,
                        losses=wl.get("losses", 0) or 0,
                        total_closed=wl.get("total", 0) or 0,
                        alltime_pnl=wl.get("total_pnl", 0.0) or 0.0,
                        today_pnl=today_pnl_row.get("pnl", 0.0) or 0.0,
                        open_positions=open_row.get("n", 0) or 0,
                        closed_today=[dict(r) for r in closed_today],
                        paper=paper,
                    )
                    daily_stats.reset_for_new_day()
                    logger.info("Midnight daily summary sent and stats reset.")
                except Exception as e:
                    logger.error("Daily summary error: %s", e)

                await asyncio.sleep(23 * 3600)  # safety — won't fire twice

        def _on_signal(signum, frame):
            logger.info("Shutdown signal %s — stopping bot...", signum)
            self._shutdown.set()

        signal.signal(signal.SIGINT,  _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        tasks = [
            asyncio.create_task(ingest_loop(),           name="ingest"),
            asyncio.create_task(track_loop(),            name="track"),
            asyncio.create_task(trade_loop(),            name="trade"),
            asyncio.create_task(hourly_heartbeat_loop(), name="hourly_heartbeat"),
            asyncio.create_task(daytime_summary_loop(),  name="daytime_summary"),
            asyncio.create_task(daily_summary_loop(),    name="daily_summary"),
        ]

        await self._shutdown.wait()
        logger.info("Cancelling background tasks...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Bot stopped cleanly.")


async def main():
    parser = argparse.ArgumentParser(description="Kalshi AI Trading Bot")
    parser.add_argument("--live", action="store_true",
                        help="Enable live trading (default: paper)")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle then exit (for testing)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(args.log_level)

    bot = TradingBot(live_mode=args.live)

    if args.once:
        await bot.startup()
        await bot.run_cycle()
        await run_evaluation(db=bot.db)
        logger.info("--once complete.")
    else:
        await bot.run_loop()


if __name__ == "__main__":
    asyncio.run(main())
