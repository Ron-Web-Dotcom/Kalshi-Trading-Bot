"""Phase 3 — real-time Kalshi market data fetcher with live logging."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.clients.kalshi_client import KalshiClient
from src.utils.database import DatabaseManager

logger = logging.getLogger("trading.market_data")


class MarketDataFetcher:
    def __init__(self, kalshi: KalshiClient, db: DatabaseManager):
        self.kalshi = kalshi
        self.db = db
        self._running = False

    async def fetch_and_store(self) -> List[Dict]:
        """Fetch all open markets, store to DB, and return them."""
        logger.info("Fetching Kalshi markets...")
        markets = await self.kalshi.get_all_markets(status="open")
        logger.info(f"Fetched {len(markets)} open markets from Kalshi")

        now = datetime.now(timezone.utc).isoformat()
        for m in markets:
            ticker = m.get("ticker", "")
            if not ticker:
                continue
            row = {
                "ticker": ticker,
                "title": m.get("title", ""),
                "category": m.get("category", ""),
                "status": m.get("status", ""),
                "yes_bid": m.get("yes_bid", 0) / 100.0 if m.get("yes_bid") else 0.0,
                "yes_ask": m.get("yes_ask", 0) / 100.0 if m.get("yes_ask") else 0.0,
                "no_bid": m.get("no_bid", 0) / 100.0 if m.get("no_bid") else 0.0,
                "no_ask": m.get("no_ask", 0) / 100.0 if m.get("no_ask") else 0.0,
                "volume": m.get("volume", 0),
                "open_interest": m.get("open_interest", 0),
                "close_time": m.get("close_time", ""),
                "last_price": m.get("last_price", 0) / 100.0 if m.get("last_price") else 0.0,
                "fetched_at": now,
            }
            await self.db.execute("""
                INSERT OR REPLACE INTO markets
                (ticker, title, category, status, yes_bid, yes_ask, no_bid, no_ask,
                 volume, open_interest, close_time, last_price, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                row["ticker"], row["title"], row["category"], row["status"],
                row["yes_bid"], row["yes_ask"], row["no_bid"], row["no_ask"],
                row["volume"], row["open_interest"], row["close_time"],
                row["last_price"], row["fetched_at"]
            ))

        # Log sample prices for live visibility
        sample = markets[:5]
        for m in sample:
            ticker = m.get("ticker", "")
            yes_ask = m.get("yes_ask", 0)
            yes_bid = m.get("yes_bid", 0)
            title = m.get("title", "")[:60]
            logger.info(
                f"[PRICE] {ticker:30s} | YES bid={yes_bid:3d}¢  ask={yes_ask:3d}¢ | {title}"
            )

        return markets

    async def get_cached_markets(self, min_volume: float = 0) -> List[Dict]:
        query = "SELECT * FROM markets WHERE status='open'"
        params: tuple = ()
        if min_volume > 0:
            query += " AND volume >= ?"
            params = (min_volume,)
        query += " ORDER BY volume DESC"
        return await self.db.fetchall(query, params)

    async def run_continuous(self, interval_seconds: int = 300):
        """Continuously fetch market data and log prices."""
        self._running = True
        while self._running:
            try:
                markets = await self.fetch_and_store()
                logger.info(f"Market data updated — {len(markets)} markets stored")
            except Exception as e:
                logger.error(f"Market data fetch error: {e}")
            await asyncio.sleep(interval_seconds)

    def stop(self):
        self._running = False
