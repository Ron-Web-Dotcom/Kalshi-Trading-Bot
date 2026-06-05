"""
Polymarket read-only client + cross-platform market comparator.

Fetches live YES/NO prices from Polymarket's public Gamma API (no key needed),
matches them against Kalshi markets by question similarity, and returns ranked
comparisons with net edge computed after Kalshi fees.

Used by:
  - arbitrage.py  → detect price gaps worth exploiting
  - opportunity.py → inject both prices into AI prompt for richer analysis
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("trading.external_markets")

# Polymarket Gamma API — public, no auth required
GAMMA_API  = "https://gamma-api.polymarket.com"
# Kalshi taker fee
KALSHI_FEE = 0.02


class PolymarketClient:
    """
    Read-only Polymarket client using the public Gamma API.

    Each market returned contains:
      question      — question text
      outcomePrices — ["0.45", "0.55"]  (YES price, NO price as fractions)
      volume        — total traded volume
      active        — bool
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=GAMMA_API,
                timeout=15,
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            )
        return self._client

    async def get_markets(self, limit: int = 200) -> List[Dict]:
        """Fetch active Polymarket markets with live prices."""
        client = await self._get()
        try:
            resp = await client.get(
                "/markets",
                params={"active": "true", "closed": "false", "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
            raw  = data if isinstance(data, list) else data.get("data", [])

            markets = []
            for m in raw:
                prices = m.get("outcomePrices") or []
                if not prices or len(prices) < 2:
                    continue
                try:
                    yes_price = float(prices[0]) * 100   # convert fraction → cents
                    no_price  = float(prices[1]) * 100
                except (TypeError, ValueError):
                    continue
                if yes_price <= 0 or yes_price >= 100:
                    continue

                markets.append({
                    "id":          m.get("id", ""),
                    "question":    m.get("question", ""),
                    "yes_price":   yes_price,
                    "no_price":    no_price,
                    "volume":      float(m.get("volume") or 0),
                    "end_date":    m.get("endDate", ""),
                    "slug":        m.get("slug", ""),
                })
            logger.debug("Polymarket: fetched %d active markets", len(markets))
            return markets
        except Exception as e:
            logger.warning("Polymarket fetch failed: %s", e)
            return []

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _keyword_overlap(a: str, b: str) -> Tuple[int, float]:
    """
    Returns (overlap_count, jaccard) between two normalised strings.
    Ignores short stopwords.
    """
    stopwords = {"will", "the", "a", "an", "in", "on", "at", "to", "of",
                 "by", "be", "is", "or", "and", "for", "it", "its", "this"}
    words_a = {w for w in _normalise(a).split() if len(w) > 2 and w not in stopwords}
    words_b = {w for w in _normalise(b).split() if len(w) > 2 and w not in stopwords}
    if not words_a or not words_b:
        return 0, 0.0
    inter = words_a & words_b
    union = words_a | words_b
    return len(inter), len(inter) / len(union)


class ExternalMarketComparator:
    """
    Compare Kalshi vs Polymarket prices.

    For each Kalshi market, finds the best-matching Polymarket question
    (requires ≥3 keyword overlap + Jaccard ≥ 0.2 to avoid false matches),
    then computes:

      - poly_yes_price / poly_no_price   — Polymarket implied probs
      - kalshi_yes / kalshi_no           — Kalshi implied probs
      - best_edge_cents                  — net edge after Kalshi fee on the
                                           cheapest side across both platforms
    """

    def __init__(self, db=None):
        self.polymarket = PolymarketClient()
        self.db         = db
        self._poly_cache: List[Dict] = []

    async def _ensure_poly(self):
        if not self._poly_cache:
            self._poly_cache = await self.polymarket.get_markets(limit=100)

    async def compare_and_log(self, kalshi_markets: List[Dict]) -> List[Dict]:
        """
        Match each Kalshi market to Polymarket. Returns comparison dicts
        sorted by edge (largest first).
        """
        await self._ensure_poly()
        results = []

        from src.utils.daily_stats import stats as daily_stats

        for km in kalshi_markets:
            ticker      = km.get("ticker", "")
            title       = km.get("title", "")
            kalshi_yes  = km.get("yes_ask", 0)
            kalshi_no   = km.get("no_ask",  0)
            if not kalshi_yes or not kalshi_no:
                continue

            match, best_jaccard, best_overlap = self._find_best_match(title, self._poly_cache)
            if not match:
                continue

            poly_yes = match["yes_price"]
            poly_no  = match["no_price"]

            # How far apart are the two platforms?
            diff_yes = abs(kalshi_yes - poly_yes)
            diff_no  = abs(kalshi_no  - poly_no)
            diff_pct = max(diff_yes, diff_no) / 100 * 100   # as percentage of $1

            # Best side to buy on Kalshi given Polymarket as reference
            if kalshi_yes < poly_yes:
                # Kalshi underprices YES → buy YES on Kalshi
                side       = "yes"
                gross_edge = poly_yes - kalshi_yes
                buy_price  = kalshi_yes
            else:
                # Kalshi overprices YES → buy NO on Kalshi
                side       = "no"
                gross_edge = poly_no - kalshi_no if poly_no > kalshi_no else kalshi_yes - poly_yes
                buy_price  = kalshi_no

            net_edge = gross_edge - buy_price * KALSHI_FEE

            comp = {
                "kalshi_ticker":   ticker,
                "kalshi_title":    title,
                "kalshi_yes":      kalshi_yes,
                "kalshi_no":       kalshi_no,
                "poly_question":   match["question"],
                "poly_yes":        poly_yes,
                "poly_no":         poly_no,
                "poly_volume":     match["volume"],
                "poly_slug":       match["slug"],
                "best_side":       side,
                "gross_edge_cents": gross_edge,
                "net_edge_cents":  net_edge,
                "diff_pct":        diff_pct,
                # Legacy fields (for arbitrage.py compat)
                "kalshi_price":    kalshi_yes,
                "poly_price":      poly_yes,
            }
            results.append(comp)

            # Determine suspicion flags
            suspicious = (
                best_jaccard < 0.30
                or best_overlap < 4
                or abs(kalshi_yes - poly_yes) > 40
            )

            # Record in daily stats tracker
            daily_stats.record_poly_match(
                ticker=ticker,
                jaccard=best_jaccard,
                net_edge=net_edge,
                suspicious=suspicious,
            )

            # Log EVERY match at INFO level; suspicious ones get an extra WARNING
            conf_tag = "[LOW-CONF ⚠️]" if suspicious else "[GOOD]"
            logger.info(
                "MATCH  %-24s  ←→  %-30s | K_YES=%g¢ P_YES=%g¢"
                " | jaccard=%.2f | net_edge=%+.1f¢  %s",
                ticker, match["slug"] or match["question"][:30],
                kalshi_yes, poly_yes, best_jaccard, net_edge, conf_tag,
            )
            if suspicious:
                logger.warning(
                    "[SUSPICIOUS MATCH] %s ←→ %s | jaccard=%.2f overlap=%d"
                    " K_YES=%.0f¢ P_YES=%.0f¢ | Review before trading",
                    ticker, match["slug"] or match["question"][:30],
                    best_jaccard, best_overlap, kalshi_yes, poly_yes,
                )

        results.sort(key=lambda x: x["net_edge_cents"], reverse=True)
        logger.info(
            "Polymarket comparison: %d matches found (%d with net edge > 0)",
            len(results),
            sum(1 for r in results if r["net_edge_cents"] > 0),
        )
        return results

    def _find_best_match(
        self, kalshi_title: str, poly_markets: List[Dict]
    ) -> Tuple[Optional[Dict], float, int]:
        """
        Find the Polymarket market most similar to a Kalshi question.

        Returns (match, best_jaccard, best_overlap).
        match is None when no candidate clears the minimum thresholds.
        """
        best, best_jaccard, best_overlap = None, 0.0, 0
        for pm in poly_markets:
            overlap, jaccard = _keyword_overlap(kalshi_title, pm["question"])
            if overlap >= 3 and jaccard > best_jaccard:
                best, best_jaccard, best_overlap = pm, jaccard, overlap
        # Require at least 20% Jaccard similarity to avoid spurious matches
        if best_jaccard < 0.20:
            return None, best_jaccard, best_overlap
        return best, best_jaccard, best_overlap

    async def close(self):
        await self.polymarket.close()
