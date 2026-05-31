"""Phase 3 — real-time Kalshi market data fetcher with live logging."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List

from src.clients.kalshi_client import KalshiClient
from src.utils.database import DatabaseManager

logger = logging.getLogger("trading.market_data")


class MarketDataFetcher:
    def __init__(self, kalshi: KalshiClient, db: DatabaseManager):
        self.kalshi = kalshi
        self.db = db
        self._running = False

    async def fetch_and_store(self) -> List[Dict]:
        """
        Fetch all open Kalshi markets, persist to DB, return raw list.

        Price convention: Kalshi API returns prices as integer cents (0–99).
        We store them AS-IS (cents) so all downstream code works in cents.
        """
        logger.info("━━━ MARKET INGEST START ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        markets = await self.kalshi.get_all_markets(status="open")
        logger.info(f"Fetched {len(markets)} open markets from Kalshi API")

        now = datetime.now(timezone.utc).isoformat()
        stored = 0
        skipped = 0

        for m in markets:
            ticker = m.get("ticker", "")
            if not ticker:
                skipped += 1
                continue

            # Prices are in cents (integers); keep them as cents
            yes_bid = m.get("yes_bid") or 0
            yes_ask = m.get("yes_ask") or 0
            no_bid  = m.get("no_bid")  or 0
            no_ask  = m.get("no_ask")  or 0
            last_price = m.get("last_price") or 0

            row = {
                "ticker":        ticker,
                "title":         m.get("title", "")[:200],
                "category":      m.get("category", ""),
                "status":        m.get("status", ""),
                "yes_bid":       float(yes_bid),
                "yes_ask":       float(yes_ask),
                "no_bid":        float(no_bid),
                "no_ask":        float(no_ask),
                "volume":        m.get("volume") or 0,
                "open_interest": m.get("open_interest") or 0,
                "close_time":    m.get("close_time", ""),
                "last_price":    float(last_price),
                "fetched_at":    now,
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
                row["last_price"], row["fetched_at"],
            ))
            stored += 1

        # Log a sample of top-volume markets for live visibility
        top = sorted(
            [m for m in markets if m.get("yes_ask") and m.get("volume", 0) > 0],
            key=lambda m: m.get("volume", 0),
            reverse=True,
        )[:8]

        if top:
            logger.info(f"{'TICKER':<32} {'YES bid':>8} {'YES ask':>8} {'vol':>8}  TITLE")
            logger.info("─" * 80)
            for m in top:
                ticker    = m.get("ticker", "")
                yes_bid   = m.get("yes_bid", 0)
                yes_ask   = m.get("yes_ask", 0)
                volume    = m.get("volume", 0)
                title     = (m.get("title", "") or "")[:40]
                logger.info(
                    f"{ticker:<32} {yes_bid:>6.0f}¢  {yes_ask:>6.0f}¢  {volume:>8,}  {title}"
                )
            logger.info("─" * 80)

        # Mark any market not in this fresh batch as closed so stale rows don't get traded
        fetched_tickers = [m.get("ticker") for m in markets if m.get("ticker")]
        if fetched_tickers:
            placeholders = ",".join("?" * len(fetched_tickers))
            await self.db.execute(
                f"UPDATE markets SET status='closed' WHERE status='open' "
                f"AND ticker NOT IN ({placeholders})",
                tuple(fetched_tickers),
            )

        logger.info(
            f"Ingest complete: {stored} stored, {skipped} skipped "
            f"(no ticker)  @{now[:19]}"
        )
        logger.info("━━━ MARKET INGEST END ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return markets

    async def get_cached_markets(self, min_volume: float = 0,
                                  max_age_minutes: int = 15) -> List[Dict]:
        """Return markets from DB fresher than max_age_minutes. Prices in cents."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
        query  = "SELECT * FROM markets WHERE status='open' AND fetched_at >= ?"
        params: tuple = (cutoff,)
        if min_volume > 0:
            query  += " AND volume >= ?"
            params += (min_volume,)
        query += " ORDER BY volume DESC"
        rows = await self.db.fetchall(query, params)
        if not rows:
            logger.warning(
                "No markets fresher than %d min — ingest may have failed. "
                "Trading cycle will be skipped.",
                max_age_minutes,
            )
        return rows

    async def run_continuous(self, interval_seconds: int = 300):
        self._running = True
        while self._running:
            try:
                markets = await self.fetch_and_store()
                logger.info(f"Market refresh: {len(markets)} markets cached")
            except Exception as e:
                logger.error(f"Market data fetch error: {e}")
            await asyncio.sleep(interval_seconds)

    def stop(self):
        self._running = False
