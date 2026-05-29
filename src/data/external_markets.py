"""Phase 4 — external market data (Polymarket read-only integration)."""

import asyncio
import logging
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.external_markets")

POLYMARKET_API = "https://clob.polymarket.com"


class PolymarketClient:
    """Read-only Polymarket CLOB API client for price comparison."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=POLYMARKET_API,
                timeout=30,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def get_markets(self, limit: int = 100) -> List[Dict]:
        client = await self._get_client()
        try:
            resp = await client.get("/markets", params={"limit": limit, "active": "true", "closed": "false"})
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            logger.warning(f"Polymarket fetch failed: {e}")
            return []

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class ExternalMarketComparator:
    """Compare Kalshi vs Polymarket prices and log differences."""

    def __init__(self, db=None):
        self.polymarket = PolymarketClient()
        self.db = db

    async def compare_and_log(self, kalshi_markets: List[Dict]) -> List[Dict]:
        """
        Match Kalshi markets to Polymarket by keyword, compute price diffs.
        Returns list of comparison results sorted by abs(diff).
        """
        poly_markets = await self.polymarket.get_markets()
        results = []

        for km in kalshi_markets:
            ticker = km.get("ticker", "")
            title = km.get("title", "").lower()
            kalshi_yes = km.get("yes_ask", 0)
            if not kalshi_yes:
                continue

            # Fuzzy match by title keywords
            best_poly = self._find_match(title, poly_markets)
            if not best_poly:
                continue

            poly_yes = best_poly.get("outcomePrices", [None, None])
            if isinstance(poly_yes, list) and poly_yes:
                try:
                    poly_price = float(poly_yes[0]) * 100  # convert to cents
                except (ValueError, TypeError):
                    continue
            else:
                continue

            diff_pct = abs(kalshi_yes - poly_price) / max(poly_price, 1) * 100
            results.append({
                "kalshi_ticker": ticker,
                "kalshi_price": kalshi_yes,
                "poly_question": best_poly.get("question", ""),
                "poly_price": poly_price,
                "diff_pct": diff_pct,
            })
            if diff_pct >= 1.0:
                logger.info(
                    f"[PRICE DIFF] {ticker} | Kalshi={kalshi_yes:.0f}¢ "
                    f"Poly={poly_price:.0f}¢ | Δ={diff_pct:.1f}%"
                )

        results.sort(key=lambda x: x["diff_pct"], reverse=True)
        return results

    def _find_match(self, kalshi_title: str, poly_markets: List[Dict]) -> Optional[Dict]:
        """Simple keyword overlap matching."""
        words = set(kalshi_title.split())
        best, best_score = None, 0
        for pm in poly_markets:
            q = pm.get("question", "").lower()
            overlap = len(words & set(q.split()))
            if overlap > best_score and overlap >= 3:
                best, best_score = pm, overlap
        return best

    async def close(self):
        await self.polymarket.close()
