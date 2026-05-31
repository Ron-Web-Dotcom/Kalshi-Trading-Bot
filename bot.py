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
from src.jobs.evaluate import run_evaluation
from src.config.settings import settings

logger = get_trading_logger("main")

# Cycle intervals (seconds)
INGEST_INTERVAL  = 300   # refresh market data every 5 min
TRACK_INTERVAL   = 120   # check position PnL every 2 min
EVAL_INTERVAL    = 300   # print performance snapshot every 5 min
TRADE_INTERVAL   = 60    # run trading cycle every 60 s
DAILY_SUMMARY_HOUR = 20  # send daily summary at 8 PM UTC


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
            )
        except Exception:
            pass

    async def run_cycle(self):
        self._cycle += 1
        logger.info(
            "┌── Trading Cycle #%d ─────────────────────────────────────",
            self._cycle,
        )
        results = await run_trading_job(db=self.db)
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

        async def eval_loop():
            await asyncio.sleep(EVAL_INTERVAL)
            while not self._shutdown.is_set():
                try:
                    await run_evaluation(db=self.db)
                except Exception as e:
                    logger.error("Eval error: %s", e)
                await asyncio.sleep(EVAL_INTERVAL)

        async def trade_loop():
            while not self._shutdown.is_set():
                try:
                    await self.run_cycle()
                except Exception as e:
                    logger.error("Trade cycle error: %s", e)
                await asyncio.sleep(TRADE_INTERVAL)

        async def daily_summary_loop():
            """Send a daily summary to Discord at DAILY_SUMMARY_HOUR UTC."""
            from src.alerts.discord import DiscordAlerter
            from datetime import datetime, timezone
            sent_today = None
            while not self._shutdown.is_set():
                now = datetime.now(timezone.utc)
                if now.hour == DAILY_SUMMARY_HOUR and sent_today != now.date():
                    try:
                        discord = DiscordAlerter()
                        today = now.date().isoformat()
                        row = await self.db.fetchone(
                            "SELECT COUNT(*) as trades, COALESCE(SUM(total_cost),0) as capital "
                            "FROM trade_logs WHERE executed_at >= ?", (today + "T00:00:00",)
                        )
                        pnl_row = await self.db.fetchone(
                            "SELECT COALESCE(SUM(pnl),0) as pnl FROM trade_logs "
                            "WHERE executed_at >= ? AND pnl IS NOT NULL", (today + "T00:00:00",)
                        )
                        open_row = await self.db.fetchone(
                            "SELECT COUNT(*) as n FROM positions WHERE status='open'"
                        )
                        trades  = (row or {}).get("trades", 0)
                        capital = (row or {}).get("capital", 0)
                        pnl     = (pnl_row or {}).get("pnl", 0)
                        open_n  = (open_row or {}).get("n", 0)
                        await discord.daily_summary(
                            date=today, trades=trades, capital=capital,
                            pnl=pnl, open_positions=open_n,
                            paper=not settings.trading.live_trading_enabled,
                        )
                        sent_today = now.date()
                    except Exception as e:
                        logger.error("Daily summary error: %s", e)
                await asyncio.sleep(60)

        def _on_signal(signum, frame):
            logger.info("Shutdown signal %s — stopping bot...", signum)
            self._shutdown.set()

        signal.signal(signal.SIGINT,  _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        tasks = [
            asyncio.create_task(ingest_loop(),        name="ingest"),
            asyncio.create_task(track_loop(),         name="track"),
            asyncio.create_task(eval_loop(),          name="eval"),
            asyncio.create_task(trade_loop(),         name="trade"),
            asyncio.create_task(daily_summary_loop(), name="daily_summary"),
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
