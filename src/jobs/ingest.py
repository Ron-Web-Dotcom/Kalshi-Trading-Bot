"""Job: ingest market data from Kalshi and Polymarket into the database."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from src.utils.junk_filter import is_junk

logger = logging.getLogger("trading.jobs.ingest")


async def run_ingestion(db_manager, market_queue: Optional[asyncio.Queue] = None,
                        run_polymarket: bool = True) -> int:
    """Fetch markets from Kalshi (always) and Polymarket (throttled). Returns total count."""
    from src.clients.kalshi_client import KalshiClient
    from src.clients.polymarket_client import PolymarketTradingClient
    from src.data.market_data import MarketDataFetcher
    from src.config.settings import settings

    kalshi = KalshiClient()
    fetcher = MarketDataFetcher(kalshi, db_manager)
    total = 0

    # ── Kalshi ────────────────────────────────────────────────────────────────
    try:
        markets = await fetcher.fetch_and_store()
        if market_queue:
            for m in markets:
                await market_queue.put(m)
        total += len(markets)
    except Exception as e:
        logger.error("Kalshi ingestion failed: %s", e)
    finally:
        await kalshi.close()

    # ── Polymarket ────────────────────────────────────────────────────────────
    if settings.polymarket.enabled and run_polymarket:
        poly = PolymarketTradingClient()
        try:
            logger.info("━━━ POLYMARKET INGEST START ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            raw_poly = await poly.get_markets(limit=500)
            now_ts   = datetime.now(timezone.utc).isoformat()
            rows = []
            for pm in raw_poly:
                try:
                    if is_junk(pm.get("title", "")):
                        continue
                    rows.append((
                        pm["ticker"], pm.get("title", "")[:200],
                        pm.get("category", ""), "open",
                        pm.get("yes_bid", 0), pm.get("yes_ask", 0),
                        pm.get("no_bid",  0), pm.get("no_ask",  0),
                        pm.get("volume",  0), 0,
                        pm.get("close_time", ""), pm.get("last_price") or pm.get("yes_ask", 0),
                        now_ts, "polymarket",
                    ))
                except Exception as e:
                    logger.debug("Polymarket ingest row error: %s", e)
            if rows:
                await db_manager.executemany("""
                    INSERT OR REPLACE INTO markets
                    (ticker, title, category, status, yes_bid, yes_ask,
                     no_bid, no_ask, volume, open_interest, close_time,
                     last_price, fetched_at, platform)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, rows)
            stored = len(rows)
            logger.info("Fetched %d Polymarket markets (%d stored)", len(raw_poly), stored)
            total += stored
        except Exception as e:
            logger.warning("Polymarket ingestion failed: %s", e)
        finally:
            await poly.close()

    return total
