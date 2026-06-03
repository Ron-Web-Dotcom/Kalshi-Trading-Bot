"""
Context builder — assembles real-world data for ANY Kalshi market before
sending it to Claude.

Data sources (all free, no API keys):
  1. Crypto prices       — CoinGecko
  2. Equity/index prices — Yahoo Finance
  3. News headlines      — AP, Reuters, BBC, CoinDesk, Politico, WSJ RSS
  4. Weather             — wttr.in (for temperature/rain/snow markets)
  5. Sports scores       — ESPN public API (for game/season markets)
  6. Economic indicators — FRED / BLS / Treasury (for CPI, Fed rate, yield markets)
  7. Community forecasts — Metaculus (optional)

Pipeline per market:
  1. Detect category (crypto / weather / sports / economics / politics / …)
  2. Extract relevant keywords / cities / teams / indicators from the title
  3. Fetch all applicable data in parallel
  4. Apply 6-second hard timeout — never block the trading cycle
  5. Return a single formatted multi-line context block for injection into AI prompt
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Tuple

from src.data.price_feeds      import get_prices_for_keywords, format_prices, CRYPTO_IDS, EQUITY_SYMBOLS
from src.data.news_fetcher     import fetch_headlines, fetch_community_prediction, format_headlines
from src.data.weather_fetcher  import extract_cities, fetch_weather, format_weather
from src.data.sports_fetcher   import fetch_sports_context, detect_league
from src.data.economic_fetcher import fetch_economic_context, detect_economic_topics

logger = logging.getLogger("trading.context_builder")

# ── Keyword extraction helpers ────────────────────────────────────────────────

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

_WEATHER_KEYWORDS = [
    "temperature", "temp", "degrees", "fahrenheit", "celsius",
    "rain", "snow", "hurricane", "tornado", "storm", "flood",
    "high", "low", "forecast", "weather", "climate",
]

_POLITICS_KEYWORDS = [
    "elect", "president", "congress", "senate", "house", "vote", "poll",
    "democrat", "republican", "governor", "mayor", "ballot", "approval",
    "trump", "biden", "harris",
]

_SPORTS_LEAGUES = ["nfl", "nba", "mlb", "nhl", "ncaa", "mls", "soccer",
                   "football", "basketball", "baseball", "hockey", "tennis",
                   "golf", "ufc", "mma", "super bowl", "world series",
                   "stanley cup", "championship", "playoffs", "draft"]


def _detect_category(ticker: str, title: str, raw_category: str) -> str:
    """Classify market into a category used to select data sources."""
    if raw_category:
        return raw_category.lower()

    t = (ticker + " " + title).lower()

    # Check most specific first
    if any(k in t for k in ["btc", "eth", "crypto", "bitcoin", "ethereum", "solana", "ripple"]):
        return "crypto"
    if any(k in t for k in _SPORTS_LEAGUES):
        return "sports"
    if any(k in t for k in _WEATHER_KEYWORDS):
        return "weather"
    if any(k in t for k in ["cpi", "inflation", "unemployment", "gdp", "fomc", "fed rate"]):
        return "economics"
    if any(k in t for k in ["sp500", "nasdaq", "stocks", "s&p", "dow", "market index"]):
        return "finance"
    if any(k in t for k in _POLITICS_KEYWORDS):
        return "politics"
    return "default"


def _extract_asset_keywords(title: str) -> Tuple[List[str], List[str]]:
    """Extract crypto and equity keyword matches from a market title."""
    t = title.lower()
    crypto, equity = [], []
    for pattern, kw in _CRYPTO_PATTERNS.items():
        if re.search(pattern, t) and kw not in crypto:
            crypto.append(kw)
    for pattern, kw in _EQUITY_PATTERNS.items():
        if re.search(pattern, t) and kw not in equity:
            equity.append(kw)
    return crypto, equity


def _news_keywords(title: str) -> List[str]:
    """Extract meaningful words for news relevance scoring."""
    stopwords = {
        "will", "the", "a", "an", "to", "by", "at", "in", "on", "of",
        "or", "and", "is", "be", "for", "end", "above", "below", "year",
        "month", "day", "week", "hit", "reach", "close", "before", "after",
        "more", "than", "over", "under", "least", "most", "price", "market",
        "does", "when", "with", "from", "this", "that", "have", "are", "was",
        "its", "not", "what", "how", "who", "any", "all", "new", "get", "set",
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
    Build a real-world context block for ANY Kalshi market.

    Automatically selects the right data sources based on market type:
      - crypto/finance  → live prices (CoinGecko / Yahoo Finance)
      - weather         → current conditions + forecast (wttr.in)
      - sports          → scores + standings (ESPN)
      - economics       → CPI, unemployment, Fed rate, yield (FRED/BLS)
      - all categories  → relevant news headlines (RSS feeds)
      - optional        → Metaculus community prediction

    Returns a formatted multi-line string ready for AI prompt injection,
    or empty string if all fetches fail / timeout.
    """
    ticker   = market.get("ticker", "")
    title    = market.get("title", "")
    raw_cat  = market.get("category", "")
    category = _detect_category(ticker, title, raw_cat)

    crypto_kws, equity_kws = _extract_asset_keywords(title)
    all_asset_kws = crypto_kws + equity_kws
    news_kws      = _news_keywords(title)

    # ── Build task list based on detected category ────────────────────────────
    tasks: Dict[str, any] = {}

    # Prices: always useful for crypto/finance; include for any market where
    # we detected asset keywords (e.g. "Will BTC exceed $50k?" in a politics market)
    if all_asset_kws:
        tasks["prices"] = get_prices_for_keywords(all_asset_kws)

    # Weather: fetch for weather markets or when a city name is in the title
    cities = extract_cities(title)
    if category == "weather" or cities:
        if cities:
            # Fetch the first matched city (usually the only one)
            tasks["weather"] = fetch_weather(cities[0])
        elif category == "weather":
            # No city detected but category says weather — try news only
            pass

    # Sports: fetch for sports markets; detect soccer specifically for news feed routing
    detected_league = detect_league(title)
    if category == "sports" or detected_league:
        tasks["sports"] = fetch_sports_context(title)
        # Use soccer-specific news feeds for soccer markets
        if detected_league in ("epl", "laliga", "bundesliga", "seriea", "ligue1",
                               "mls", "ucl", "uel", "uecl", "worldcup", "euros",
                               "copalibertadores", "concacaf", "nwsl"):
            category = "soccer"

    # Economics: fetch for econ/finance markers with indicator keywords
    if category in ("economics", "finance") or detect_economic_topics(title):
        tasks["economics"] = fetch_economic_context(title)

    # News: always fetch — relevant to every market type
    tasks["headlines"] = fetch_headlines(news_kws, category=category, max_headlines=5)

    # Metaculus: optional community prediction
    if include_community:
        tasks["community"] = fetch_community_prediction(title)

    # ── Execute all fetches in parallel with hard timeout ─────────────────────
    try:
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

    # ── Assemble context blocks ───────────────────────────────────────────────
    blocks = []

    prices = results.get("prices")
    if isinstance(prices, list) and prices:
        blocks.append(format_prices(prices))

    weather = results.get("weather")
    if isinstance(weather, dict) and weather:
        blocks.append(format_weather(weather))

    sports = results.get("sports")
    if isinstance(sports, str) and sports:
        blocks.append(sports)

    economics = results.get("economics")
    if isinstance(economics, str) and economics:
        blocks.append(economics)

    headlines = results.get("headlines")
    if isinstance(headlines, list) and headlines:
        blocks.append(format_headlines(headlines))

    community = results.get("community")
    if isinstance(community, str) and community:
        blocks.append(community)

    if blocks:
        context = "\n\n".join(blocks)
        logger.debug(
            "Context for %s [%s]: %d block(s) — %s",
            ticker, category,
            len(blocks),
            ", ".join(k for k in results if not isinstance(results[k], Exception) and results[k]),
        )
        return context

    return ""
