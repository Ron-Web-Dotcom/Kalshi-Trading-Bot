"""Phase 3 — real-time Kalshi market data fetcher with live logging."""

import asyncio
import logging
from datetime import datetime
from src.utils.junk_filter import is_junk
from typing import Dict, List

from src.clients.kalshi_client import KalshiClient
from src.utils.database import DatabaseManager
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

logger = logging.getLogger("trading.market_data")


class MarketDataFetcher:
    def __init__(self, kalshi: KalshiClient, db: DatabaseManager):
        self.kalshi = kalshi
        self.db = db
        self._running = False

    # All Kalshi series prefixes — covers every sport, category, and sub-category
    _KALSHI_SERIES = [
        # Sports
        "NFL", "NBA", "MLB", "NHL", "SOCCER", "FIFA", "MLS", "UEFA",
        "UFC", "MMA", "BOXING", "TENNIS", "GOLF", "PGA", "F1", "NASCAR",
        "RUGBY", "CRICKET", "WNBA", "NCAAF", "NCAAB", "ESPORTS",
        "EPL", "LALIGA", "BUNDESLIGA", "SERIEA", "LIGUE1",
        # Politics / Elections
        "ELECTION", "VOTE", "SENATE", "HOUSE", "CONGRESS", "PRESIDENT",
        "TRUMP", "BIDEN", "SCOTUS", "GOVERNOR",
        # Finance / Economics
        "FED", "FOMC", "CPI", "GDP", "JOBS", "INFLATION",
        "SP500", "NASDAQ", "STOCKS", "OIL", "GOLD", "SILVER", "FOREX",
        # Crypto
        "BTC", "ETH", "SOL", "CRYPTO",
        # Geopolitics / World
        "IRAN", "RUSSIA", "UKRAINE", "CHINA", "TAIWAN", "NATO",
        "ISRAEL", "NORTHKOREA", "INDIA",
        # Tech / Science
        "SPACEX", "NASA", "AI", "OPENAI", "APPLE", "GOOGLE", "META",
        # Weather
        "HURRICANE", "TORNADO", "WEATHER",
        # Culture / Entertainment
        "OSCARS", "GRAMMYS", "EMMYS",
        # Misc
        "DEBATE", "HEARING", "FED",
    ]

    async def fetch_and_store(self) -> List[Dict]:
        """
        Fetch ALL open Kalshi markets across every category and series.
        Sweeps: /markets by volume, /markets by close_time, /events,
        and every sport/category series prefix in parallel.
        """
        logger.info("━━━ MARKET INGEST START ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # Fetch by volume, by close_time, events endpoint, and all series in parallel
        async def _fetch_series(prefix: str) -> List[Dict]:
            try:
                data = await self.kalshi._request("GET", "/markets", params={
                    "status": "open", "series_ticker": prefix, "limit": 100,
                })
                return data.get("markets") or [] if isinstance(data, dict) else []
            except Exception:
                return []

        async def _fetch_events() -> List[Dict]:
            try:
                data = await self.kalshi._request("GET", "/events", params={"status": "open", "limit": 200})
                result = []
                for ev in (data.get("events") or []):
                    for m in (ev.get("markets") or []):
                        if isinstance(m, dict):
                            m.setdefault("category", ev.get("category", ""))
                            m.setdefault("title", ev.get("title", ""))
                            result.append(m)
                return result
            except Exception:
                return []

        vol_task, close_task, events_task, *series_tasks = await asyncio.gather(
            self.kalshi.get_all_markets(status="open", max_markets=300),
            self.kalshi.get_all_markets(status="open", max_markets=100, sort_by_close=True),
            _fetch_events(),
            *[_fetch_series(p) for p in self._KALSHI_SERIES],
            return_exceptions=True,
        )

        # Merge all — deduplicate by ticker
        seen: set = set()
        markets: List[Dict] = []
        for source in [vol_task, close_task, events_task, *series_tasks]:
            if not isinstance(source, list):
                continue
            for m in source:
                if not isinstance(m, dict):
                    continue
                ticker = m.get("ticker", "")
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    markets.append(m)

        logger.info("Fetched %d unique open markets from Kalshi (all categories + series)", len(markets))

        now = datetime.now(_ET).isoformat()
        stored = 0
        skipped = 0

        # Batch insert for speed — one transaction instead of N round-trips
        rows = []
        for m in markets:
            ticker = m.get("ticker", "")
            if not ticker:
                skipped += 1
                continue
            if is_junk(m.get("title", "")):
                skipped += 1
                continue
            rows.append((
                ticker,
                (m.get("title", "") or "")[:200],
                m.get("category", ""),
                "open",  # force 'open' — Kalshi API may return 'active' or other values
                float(m.get("yes_bid") or 0),
                float(m.get("yes_ask") or 0),
                float(m.get("no_bid")  or 0),
                float(m.get("no_ask")  or 0),
                m.get("volume") or 0,
                m.get("open_interest") or 0,
                m.get("close_time", ""),
                float(m.get("last_price") or 0),
                now,
                "kalshi",
            ))

        if rows:
            await self.db.executemany("""
                INSERT OR REPLACE INTO markets
                (ticker, title, category, status, yes_bid, yes_ask, no_bid, no_ask,
                 volume, open_interest, close_time, last_price, fetched_at, platform)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)
            stored = len(rows)

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
                yes_bid   = float(m.get("yes_bid") or 0)
                yes_ask   = float(m.get("yes_ask") or 0)
                volume    = m.get("volume", 0)
                title     = (m.get("title", "") or "")[:40]
                logger.info(
                    f"{ticker:<32} {yes_bid:>6.0f}¢  {yes_ask:>6.0f}¢  {volume:>8,}  {title}"
                )
            logger.info("─" * 80)

        # Mark stale markets as closed (ticker-based exclusion avoids closing rows
        # written in the same second or by concurrent processes)
        if rows:
            fetched_tickers = [r[0] for r in rows]  # ticker is index 0
            chunk_size = 900
            for i in range(0, len(fetched_tickers), chunk_size):
                chunk = fetched_tickers[i:i + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                await self.db.execute(
                    f"UPDATE markets SET status='closed' WHERE status='open' AND ticker NOT IN ({placeholders}) AND (platform='kalshi' OR platform IS NULL)",
                    chunk
                )

        # Purge closed markets older than 7 days to prevent unbounded DB growth
        await self.db.execute(
            "DELETE FROM markets WHERE status='closed' AND fetched_at < datetime('now', '-7 days')"
        )

        logger.info(
            f"Ingest complete: {stored} stored, {skipped} skipped "
            f"(no ticker)  @{now[:19]}"
        )
        logger.info("━━━ MARKET INGEST END ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return markets

    async def get_cached_markets(self, min_volume: float = 0,
                                  max_age_minutes: int = 15,
                                  limit: int = 200) -> List[Dict]:
        """Return markets from DB. Prices in cents.

        Fetches Kalshi and Polymarket separately so high-volume Polymarket
        markets never crowd out Kalshi (which reports volume in cents, not USD).
        """
        base = "SELECT * FROM markets WHERE status='open' OR status=''"
        vol_clause = " AND volume >= ?" if min_volume > 0 else ""
        vol_params: tuple = (min_volume,) if min_volume > 0 else ()

        kalshi_q  = base + " AND (platform='kalshi' OR platform IS NULL)" + vol_clause
        kalshi_q += f" ORDER BY volume DESC LIMIT {int(limit)}"
        poly_q    = base + " AND platform='polymarket'" + vol_clause
        poly_q   += f" ORDER BY volume DESC LIMIT {int(limit)}"

        kalshi_rows = await self.db.fetchall(kalshi_q, vol_params) or []
        poly_rows   = await self.db.fetchall(poly_q,   vol_params) or []
        rows = kalshi_rows + poly_rows

        if not rows:
            logger.warning("No markets in cache — DB may be empty on first startup")

        logger.info(
            "get_cached_markets: %d Kalshi + %d Polymarket = %d total (min_vol=%g)",
            len(kalshi_rows), len(poly_rows), len(rows), min_volume,
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
