"""
Context builder — assembles real-world data for a Kalshi market before
sending it to Claude.

For each market:
  1. Detect what type it is (crypto / politics / finance / sports / …)
  2. Extract relevant keywords from the title
  3. Fetch live prices for those assets (CoinGecko / Yahoo Finance)
  4. Fetch relevant headlines (RSS feeds)
  5. (Optional) Check Metaculus community prediction
  6. Return a formatted context block ready for injection into the AI prompt

The whole fetch is parallelised and has a hard timeout so a slow feed
never blocks the trading cycle.
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Tuple

from src.data.price_feeds import get_prices_for_keywords, format_prices, CRYPTO_IDS, EQUITY_SYMBOLS
from src.data.news_fetcher import fetch_headlines, fetch_community_prediction, format_headlines

logger = logging.getLogger("trading.context_builder")

# ── Keyword extraction helpers ────────────────────────────────────────────────

# Map of title phrases → asset keywords for price lookup
_CRYPTO_PATTERNS = {
    r"\bbtc\b|\bbitcoin\b":   "btc",
    r"\beth\b|\bethereum\b":  "eth",
    r"\bsol\b|\bsolana\b":    "sol",
    r"\bxrp\b|\bripple\b":    "xrp",
    r"\bdoge\b|\bdogecoin\b": "doge",
    r"\bbnb\b":               "bnb",
    r"\bada\b|\bcardano\b":   "ada",
    r"\bavax\b|\bavalanche\b": "avax",
    r"\bmatic\b|\bpolygon\b": "matic",
}

_EQUITY_PATTERNS = {
    r"\bs.?p\s*500\b|\bspx\b|\bsp500\b":    "sp500",
    r"\bnasdaq\b|\bndx\b|\bqqq\b":          "nasdaq",
    r"\bdow\b|\bdjia\b":                    "dow",
    r"\bvix\b|\bvolatility\b":              "vix",
    r"\boil\b|\bcrude\b|\bwti\b":           "oil",
    r"\bgold\b":                            "gold",
    r"\btreasur|fed.rate|10.year|yield\b":  "10y",
    r"\beur\b|\beuro\b":                    "eur",
}

_CATEGORY_MAP = {
    "crypto":     ["btc", "eth", "sol", "xrp", "doge", "bnb", "ada", "avax"],
    "finance":    ["sp500", "nasdaq", "dow", "vix", "oil", "gold", "10y"],
    "economics":  ["sp500", "10y", "oil", "gold"],
    "politics":   [],   # no price feeds — rely on news
    "sports":     [],
    "weather":    [],
    "technology": [],
    "health":     [],
}


def _detect_category(ticker: str, title: str, raw_category: str) -> str:
    """Best-effort category from raw Kalshi category + ticker prefix."""
    cat = (raw_category or "").lower()
    if cat:
        return cat
    t = (ticker + " " + title).lower()
    if any(c in t for c in ["btc", "eth", "crypto", "bitcoin", "ethereum"]):
        return "crypto"
    if any(c in t for c in ["sp500", "nasdaq", "stocks", "fed", "rate", "inflation"]):
        return "economics"
    if any(c in t for c in ["elect", "president", "congress", "senate", "vote", "poll"]):
        return "politics"
    if any(c in t for c in ["nfl", "nba", "mlb", "nhl", "soccer", "world cup"]):
        return "sports"
    return "default"


def _extract_asset_keywords(title: str) -> Tuple[List[str], List[str]]:
    """
    Extract crypto and equity keyword matches from a market title.
    Returns (crypto_keywords, equity_keywords).
    """
    t = title.lower()
    crypto = []
    equity = []
    for pattern, kw in _CRYPTO_PATTERNS.items():
        if re.search(pattern, t) and kw not in crypto:
            crypto.append(kw)
    for pattern, kw in _EQUITY_PATTERNS.items():
        if re.search(pattern, t) and kw not in equity:
            equity.append(kw)
    return crypto, equity


def _news_keywords(title: str) -> List[str]:
    """
    Extract meaningful words for news relevance scoring.
    Strips common filler words.
    """
    stopwords = {
        "will", "the", "a", "an", "to", "by", "at", "in", "on", "of",
        "or", "and", "is", "be", "for", "end", "above", "below", "year",
        "month", "day", "week", "hit", "reach", "close", "before", "after",
        "more", "than", "over", "under", "least", "most", "price", "market",
        "does", "when", "with", "from", "this", "that", "have", "are", "was",
    }
    words = re.findall(r"[a-zA-Z]{3,}", title)
    return [w.lower() for w in words if w.lower() not in stopwords][:12]


# ── Main context builder ──────────────────────────────────────────────────────

async def build_market_context(
    market: Dict,
    include_community: bool = False,
    timeout_seconds: float = 6.0,
) -> str:
    """
    Build a real-world context block for a Kalshi market.

    Args:
        market           : market dict from DB (ticker, title, category, …)
        include_community: also fetch Metaculus community prediction
        timeout_seconds  : hard deadline — return empty string on timeout

    Returns:
        Formatted multi-line string ready to inject into the AI prompt.
    """
    ticker   = market.get("ticker", "")
    title    = market.get("title", "")
    raw_cat  = market.get("category", "")
    category = _detect_category(ticker, title, raw_cat)

    crypto_kws, equity_kws = _extract_asset_keywords(title)
    all_asset_kws = crypto_kws + equity_kws
    news_kws      = _news_keywords(title)

    try:
        tasks = {
            "prices":    get_prices_for_keywords(all_asset_kws),
            "headlines": fetch_headlines(news_kws, category=category, max_headlines=5),
        }
        if include_community:
            tasks["community"] = fetch_community_prediction(title)

        # Hard timeout — don't block trading cycle
        gathered = await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True),
            timeout=timeout_seconds,
        )
        results = dict(zip(tasks.keys(), gathered))
    except asyncio.TimeoutError:
        logger.warning("Context fetch timed out for %s — proceeding without context", ticker)
        return ""
    except Exception as e:
        logger.debug("Context fetch error for %s: %s", ticker, e)
        return ""

    blocks = []

    prices    = results.get("prices", [])
    headlines = results.get("headlines", [])
    community = results.get("community")

    if isinstance(prices, list) and prices:
        blocks.append(format_prices(prices))

    if isinstance(headlines, list) and headlines:
        blocks.append(format_headlines(headlines))

    if community and isinstance(community, str):
        blocks.append(community)

    if blocks:
        context = "\n\n".join(blocks)
        logger.debug(
            "Context for %s: %d price(s), %d headline(s)",
            ticker,
            len(prices) if isinstance(prices, list) else 0,
            len(headlines) if isinstance(headlines, list) else 0,
        )
        return context

    return ""
