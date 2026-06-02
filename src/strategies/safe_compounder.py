"""Safe compounder — conservative NO-side math-only strategy."""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("trading.safe_compounder")


class SafeCompounder:
    """
    Finds markets where NO is priced above fair value.
    No LLM required — pure math edge detection.
    """

    def __init__(self, min_edge_pct: float = 3.0, min_volume: int = 100,
                 client=None, dry_run: bool = True):
        self.min_edge_pct = min_edge_pct
        self.min_volume = min_volume
        self.client = client
        self.dry_run = dry_run

    async def run(self) -> List[Dict]:
        """Fetch markets, find opportunities, log them (dry run = no orders)."""
        if self.client is None:
            logger.warning("SafeCompounder: no Kalshi client — cannot run")
            return []
        try:
            raw = await self.client.get_all_markets()
            markets = []
            for m in raw:
                markets.append({
                    "ticker": m.get("ticker", ""),
                    "title": m.get("title", ""),
                    "yes_ask": m.get("yes_ask", 0),
                    "no_ask": m.get("no_ask", 0),
                    "volume": m.get("volume", 0),
                })
            opps = self.find_opportunities(markets)
            for opp in opps[:10]:
                action = "DRY-RUN" if self.dry_run else "BUY NO"
                logger.info(
                    f"[SAFE-COMPOUNDER] {action} {opp['ticker']} | "
                    f"edge={opp['edge_pct']:.1f}¢ | NO={opp['no_ask']:.0f}¢ | {opp['title']}"
                )
            return opps
        except Exception as e:
            logger.error(f"SafeCompounder.run() error: {e}")
            return []

    def find_opportunities(self, markets: List[Dict]) -> List[Dict]:
        """Return markets where NO ask price implies edge >= min_edge_pct."""
        opportunities = []
        for m in markets:
            if m.get("volume", 0) < self.min_volume:
                continue
            no_ask = m.get("no_ask", 0)
            yes_ask = m.get("yes_ask", 0)
            if no_ask <= 0 or yes_ask <= 0:
                continue
            implied_prob = no_ask / 100  # probability NO resolves
            # Edge: market sum > 100 or implied probability seems high
            spread_sum = yes_ask + no_ask
            if spread_sum > 100:
                edge = spread_sum - 100
                if edge >= self.min_edge_pct:
                    opportunities.append({
                        "ticker": m.get("ticker"),
                        "title": m.get("title", "")[:60],
                        "no_ask": no_ask,
                        "yes_ask": yes_ask,
                        "edge_pct": edge,
                        "volume": m.get("volume", 0),
                    })
        return sorted(opportunities, key=lambda x: x["edge_pct"], reverse=True)
