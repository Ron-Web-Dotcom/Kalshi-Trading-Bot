#!/usr/bin/env python3
"""
Kalshi AI Trading Bot — main entry point.

Usage:
    python bot.py               # Paper trading (default, safe)
    python bot.py --live        # Live trading (requires LIVE_TRADING_ENABLED=true)
    python bot.py --once        # Run one cycle and exit
    python bot.py --log-level DEBUG
"""

import asyncio
import argparse
import logging
import signal
import sys
from datetime import datetime, timedelta

from src.utils.logging_setup import setup_logging, get_trading_logger
from src.utils.database import DatabaseManager
from src.clients.kalshi_client import KalshiClient
from src.data.market_data import MarketDataFetcher
from src.jobs.ingest import run_ingestion
from src.jobs.trade import run_trading_job
from src.jobs.track import run_tracking
from src.jobs.evaluate import run_evaluation
from src.config.settings import settings

logger = get_trading_logger("main")


class TradingBot:
    def __init__(self, live_mode: bool = False):
        # Safety: live mode requires explicit env var AND CLI flag
        if live_mode and not settings.trading.live_trading_enabled:
            print("ERROR: --live flag requires LIVE_TRADING_ENABLED=true in .env")
            sys.exit(1)

        settings.trading.live_trading_enabled = live_mode
        settings.trading.paper_trading_mode = not live_mode

        self.live_mode = live_mode
        self.db = DatabaseManager()
        self._shutdown = asyncio.Event()

        if live_mode:
            logger.warning("=" * 60)
            logger.warning("  LIVE TRADING MODE — REAL MONEY WILL BE USED")
            logger.warning("=" * 60)
        else:
            logger.info("Paper trading mode — no real money at risk")

    async def startup(self):
        logger.info("Initializing database...")
        await self.db.initialize()
        logger.info("Running initial market ingestion...")
        try:
            count = await run_ingestion(self.db)
            logger.info(f"Ingested {count} markets. Bot is ready.")
        except Exception as e:
            logger.warning(f"Initial ingestion failed (will retry in loop): {e}")

    async def run_cycle(self):
        results = await run_trading_job(db=self.db)
        logger.info(
            f"Cycle done — positions={results.total_positions} "
            f"capital_used=${results.total_capital_used:.2f}"
        )

    async def run_loop(self):
        await self.startup()
        cycle = 0

        # Background tasks
        async def ingest_loop():
            while not self._shutdown.is_set():
                try:
                    await run_ingestion(self.db)
                except Exception as e:
                    logger.error(f"Ingestion error: {e}")
                await asyncio.sleep(300)

        async def track_loop():
            while not self._shutdown.is_set():
                try:
                    await run_tracking(self.db)
                except Exception as e:
                    logger.error(f"Tracking error: {e}")
                await asyncio.sleep(120)

        async def eval_loop():
            while not self._shutdown.is_set():
                try:
                    await run_evaluation(self.db)
                except Exception as e:
                    logger.error(f"Evaluation error: {e}")
                await asyncio.sleep(300)

        async def trade_loop():
            nonlocal cycle
            while not self._shutdown.is_set():
                try:
                    cycle += 1
                    logger.info(f"--- Trading Cycle #{cycle} ---")
                    await self.run_cycle()
                except Exception as e:
                    logger.error(f"Trade cycle error: {e}")
                await asyncio.sleep(60)

        def _handle_signal(signum, frame):
            logger.info(f"Shutdown signal {signum} received")
            self._shutdown.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        tasks = [
            asyncio.create_task(ingest_loop()),
            asyncio.create_task(track_loop()),
            asyncio.create_task(eval_loop()),
            asyncio.create_task(trade_loop()),
        ]

        await self._shutdown.wait()
        logger.info("Shutting down...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Bot stopped.")


async def main():
    parser = argparse.ArgumentParser(description="Kalshi AI Trading Bot")
    parser.add_argument("--live", action="store_true",
                        help="Enable live trading (default: paper)")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle and exit")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(args.log_level)

    bot = TradingBot(live_mode=args.live)

    if args.once:
        await bot.startup()
        await bot.run_cycle()
        await run_evaluation(bot.db)
    else:
        await bot.run_loop()


if __name__ == "__main__":
    asyncio.run(main())
