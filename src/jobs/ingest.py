"""Job: ingest market data from Kalshi into the database."""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("trading.jobs.ingest")


async def run_ingestion(db_manager, market_queue: Optional[asyncio.Queue] = None) -> int:
    """Fetch all Kalshi markets and store them. Returns count of markets fetched."""
    from src.clients.kalshi_client import KalshiClient
    from src.data.market_data import MarketDataFetcher

    kalshi = KalshiClient()
    fetcher = MarketDataFetcher(kalshi, db_manager)
    try:
        markets = await fetcher.fetch_and_store()
        if market_queue:
            for m in markets:
                await market_queue.put(m)
        return len(markets)
    except Exception as e:
        logger.error(f"Ingestion failed: {e}")
        return 0
    finally:
        await kalshi.close()
