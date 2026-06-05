"""
Category-aware market scanner — sweeps EVERY category AND sub-category
on both Polymarket and Kalshi in parallel.

Polymarket categories + sub-categories covered:
  Politics     → US Elections, Senate, House, Governor, UK, EU, Global, Midterms
  Sports       → NFL, NBA, MLB, NHL, Soccer/Football, Tennis, Golf, UFC, F1,
                 EPL, La Liga, Bundesliga, Serie A, Ligue 1, Champions League,
                 World Cup, Olympics, College Sports, Esports (LoL, CS2, Dota2)
  Crypto       → Bitcoin, Ethereum, Solana, Altcoins, DeFi, NFT, ETF, Stablecoins
  Finance      → Stocks, S&P500, Nasdaq, Oil, Gold, Silver, Forex, Interest Rates
  Science/Tech → SpaceX, NASA, AI, OpenAI, Elon Musk, Apple, Google, Meta
  Geopolitics  → Iran, Russia, Ukraine, Middle East, China, NATO, Taiwan, North Korea
  Economics    → Inflation, CPI, Fed, GDP, Jobs, Unemployment, Recession
  Weather      → Hurricanes, Tornadoes, Earthquakes, Climate, Temperature
  Culture      → Oscars, Emmys, Grammys, Movies, TV, Music, Books, Gaming
  Health       → FDA, Drug Approvals, Pandemic, WHO, Vaccines
  World        → UN, G7, G20, International Events, Conflicts
  Misc         → Celebrity, Pop Culture, Viral Events, Reddit/Mentions

Strategy:
  1. Fetch all tags/categories in parallel from Polymarket Gamma API
  2. Pull ALL Kalshi categories from DB
  3. Pre-score every market (price, volume, time-to-close)
  4. Return diverse ranked list across all categories for AI evaluation
"""

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.category_scanner")

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_TIMEOUT    = httpx.Timeout(15.0)
_HEADERS    = {
    "User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)",
    "Accept": "application/json",
}

# ── ALL Polymarket tag slugs — main categories + all sub-categories ────────────

POLY_TAG_SLUGS = [
    # Politics — US + global
    "politics", "us-politics", "elections", "trump", "congress", "senate",
    "supreme-court", "2024-us-elections", "geopolitics", "middle-east",
    "russia-ukraine", "china", "iran",
    # Sports — major leagues
    "nfl", "nba", "mlb", "nhl", "ufc", "soccer", "tennis", "golf",
    # Sports — more
    "mma", "boxing", "college-football", "college-basketball", "f1",
    "nascar", "esports", "olympics", "wrestling",
    # Crypto — broad
    "crypto", "bitcoin", "ethereum", "solana", "defi", "nft",
    # Finance / Economy
    "finance", "stocks", "fed", "economy", "interest-rates", "inflation",
    "commodities", "oil", "gold",
    # Entertainment / Culture
    "entertainment", "pop-culture", "music", "movies", "awards", "celebrity",
    "tv", "gaming",
    # Science / Tech
    "science", "technology", "ai", "space", "spacex",
    # World / Weather
    "world", "weather", "climate", "natural-disasters",
    # Misc high-volume
    "business", "health", "pandemic", "media",
]

# Remove duplicates while preserving order
_seen = set()
POLY_TAG_SLUGS = [
    t for t in POLY_TAG_SLUGS
    if t not in _seen and not _seen.add(t)
]

# ── Kalshi category keywords for DB search ───────────────────────────────────

KALSHI_CATEGORY_PATTERNS = [
    # Main categories
    "elections", "politics", "sports", "crypto", "finance", "economics",
    "climate", "weather", "tech", "science", "culture", "health", "world",
    "commodities", "entertainment", "mentions",
    # Sub-categories / keywords in titles
    "nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball",
    "baseball", "hockey", "ufc", "tennis", "golf",
    "bitcoin", "ethereum", "solana", "defi",
    "inflation", "fed", "interest", "cpi", "gdp", "jobs",
    "hurricane", "tornado", "earthquake", "wildfire",
    "spacex", "nasa", "ai", "election", "senate", "house",
    "president", "trump", "iran", "russia", "china",
    "oscar", "grammy", "emmy", "movie", "music",
]


async def _fetch_poly_tag(tag: str, limit: int = 30) -> List[Dict]:
    """Fetch active Polymarket markets for a single tag slug. Returns [] on any error."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS,
                                     follow_redirects=True) as client:
            r = await client.get(
                f"{_GAMMA_BASE}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "tag_slug": tag,
                    "limit": limit,
                    "order": "volume",
                    "ascending": "false",
                },
            )
            if r.status_code not in (200, 201):
                return []
            raw = r.json()
        items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])
    except Exception as e:
        logger.debug("Poly tag '%s' failed: %s", tag, e)
        return []

    markets = []
    for m in items:
        parsed = _parse_poly_market(m, tag)
        if parsed:
            markets.append(parsed)
    return markets


async def _fetch_poly_bulk(limit: int = 500) -> List[Dict]:
    """Bulk fetch without tag filter — catches markets not tagged with a slug."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS,
                                     follow_redirects=True) as client:
            r = await client.get(
                f"{_GAMMA_BASE}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "order": "volume",
                    "ascending": "false",
                },
            )
            if r.status_code != 200:
                return []
            raw = r.json()
        items = raw if isinstance(raw, list) else (raw.get("data") or [])
    except Exception as e:
        logger.debug("Poly bulk fetch failed: %s", e)
        return []

    return [m for m in [_parse_poly_market(i, "") for i in items] if m]


def _parse_poly_market(m: Dict, tag: str) -> Optional[Dict]:
    """Parse a raw Polymarket market object. Returns None if unparseable/invalid."""
    import json as _j
    try:
        raw_prices = m.get("outcomePrices") or []
        if isinstance(raw_prices, str):
            raw_prices = _j.loads(raw_prices)

        yes_price, no_price = 50.0, 50.0
        if len(raw_prices) >= 2:
            p0, p1 = float(raw_prices[0]), float(raw_prices[1])
            yes_price = p0 * 100 if p0 <= 1.0 else p0
            no_price  = p1 * 100 if p1 <= 1.0 else p1
        elif len(raw_prices) == 1:
            p0 = float(raw_prices[0])
            yes_price = p0 * 100 if p0 <= 1.0 else p0
            no_price  = 100 - yes_price

        # Fallback to bestAsk
        if yes_price == 0:
            ask = float(m.get("bestAsk") or m.get("lastTradePrice") or 0)
            yes_price = ask * 100 if ask <= 1.0 else ask
            no_price  = 100 - yes_price

        if yes_price < 2 or yes_price > 98:
            return None

        ticker = str(m.get("conditionId") or m.get("id") or m.get("slug") or "").strip()
        if not ticker:
            question = m.get("question") or m.get("description") or ""
            if not question:
                return None
            import hashlib
            ticker = "poly_" + hashlib.md5(question.encode()).hexdigest()[:12]

        title = (m.get("question") or m.get("description") or m.get("title") or "").strip()
        if not title or len(title) < 5:
            return None

        volume = 0.0
        try:
            volume = float(m.get("volume") or m.get("volumeNum") or 0)
        except (TypeError, ValueError):
            pass

        # Try to get token IDs for order placement
        token_ids = m.get("clobTokenIds") or m.get("tokenIds") or []

        return {
            "platform":    "polymarket",
            "ticker":      ticker,
            "title":       title,
            "category":    (m.get("category") or tag or "").lower(),
            "_scan_cat":   tag or (m.get("category") or "").lower(),
            "yes_ask":     round(yes_price, 1),
            "no_ask":      round(no_price, 1),
            "yes_bid":     round(max(yes_price - 1, 1), 1),
            "no_bid":      round(max(no_price  - 1, 1), 1),
            "volume":      volume,
            "close_time":  m.get("endDate", ""),
            "last_price":  yes_price,
            "open_interest": 0,
            "_yes_token":  token_ids[0] if len(token_ids) > 0 else "",
            "_no_token":   token_ids[1] if len(token_ids) > 1 else "",
        }
    except Exception:
        return None


def _pre_score(market: Dict) -> float:
    """
    Fast rule-based pre-score — no API calls.
    Higher = more promising for AI evaluation.
    Factors: price distance from 50¢, volume, time-to-close.
    """
    yes_ask = float(market.get("yes_ask") or market.get("last_price") or 0)
    no_ask  = float(market.get("no_ask")  or (100 - yes_ask) if yes_ask else 0)
    volume  = float(market.get("volume") or 0)

    if yes_ask <= 1 or yes_ask >= 99:
        return 0.0

    # Markets near 50¢ have most edge potential on either side
    price_score = 1.0 - abs(yes_ask - 50) / 50.0

    # Volume = pricing signal confidence
    liquidity = min(volume / 1000.0, 1.0)
    if volume == 0:
        liquidity = 0.03

    # Time bonus — sweet spot is 1–48h to close
    time_bonus = 1.0
    ct = market.get("close_time", "")
    if ct:
        try:
            close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
            hours = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if 0.1 < hours <= 1:      time_bonus = 1.6  # very soon
            elif 1 < hours <= 6:      time_bonus = 2.0  # sweet spot — live/near-live
            elif 6 < hours <= 24:     time_bonus = 1.5  # today
            elif 24 < hours <= 72:    time_bonus = 1.2  # this week
            elif hours <= 0:          time_bonus = 0.0  # already closed
        except Exception:
            pass

    # Spread penalty — wide spread = illiquid
    spread = abs(yes_ask + no_ask - 100)
    spread_factor = max(0.5, 1.0 - spread / 25.0)

    return price_score * max(liquidity, 0.03) * time_bonus * spread_factor


def _normalize_cat(cat: str) -> str:
    """Normalize category strings to a clean canonical form."""
    cat = (cat or "").lower().strip()
    _map = {
        "econ": "economics", "financial": "finance", "pol": "politics",
        "election": "elections", "sport": "sports", "tech": "tech",
        "technology": "tech", "sci": "science", "scientific": "science",
        "crypto": "crypto", "cryptocurrency": "crypto", "weather": "weather",
        "climate": "climate", "commodity": "commodities",
        "entertainment": "culture", "health": "health", "world": "world",
        "geo": "geopolitics", "geopolitical": "geopolitics",
    }
    for k, v in _map.items():
        if k in cat:
            return v
    return cat or "general"


def _diverse_top(markets: List[Dict], total: int, top_per_cat: int = 4) -> List[Dict]:
    """
    Pick `total` markets with category diversity.
    Takes top_per_cat from each category first, then fills by score.
    """
    by_cat: Dict[str, List[Dict]] = defaultdict(list)
    for m in markets:
        cat = m.get("_scan_cat") or m.get("category") or "general"
        by_cat[cat].append(m)

    selected: List[Dict] = []
    seen: set = set()

    # First pass: take top_per_cat per category
    for cat in sorted(by_cat.keys()):
        for m in by_cat[cat][:top_per_cat]:
            t = m.get("ticker", "")
            if t and t not in seen:
                selected.append(m)
                seen.add(t)

    # Second pass: fill remaining slots by pre_score
    for m in markets:
        if len(selected) >= total:
            break
        t = m.get("ticker", "")
        if t and t not in seen:
            selected.append(m)
            seen.add(t)

    selected.sort(key=lambda m: m.get("_pre_score", 0), reverse=True)
    return selected[:total]


class CategoryScanner:
    """
    Sweeps ALL categories and sub-categories on both Polymarket and Kalshi.
    Returns a ranked list of candidates ready for AI evaluation.
    """

    def __init__(self, db=None):
        self.db = db

    async def _kalshi_all_categories(self, max_per_cat: int = 15) -> List[Dict]:
        """Pull ALL Kalshi markets from DB organized by category."""
        if not self.db:
            return []
        try:
            rows = await self.db.fetchall(
                "SELECT ticker, title, category, yes_ask, no_ask, yes_bid, no_bid, "
                "volume, open_interest, close_time, last_price, platform "
                "FROM markets "
                "WHERE (status='open' OR status='') "
                "AND (platform='kalshi' OR platform IS NULL) "
                "AND (yes_ask > 0 OR last_price > 0) "
                "AND title IS NOT NULL AND title != '' "
                "ORDER BY volume DESC"
            ) or []

            def _norm_price(v):
                try:
                    f = float(v or 0)
                    return f if f <= 1.0 else f / 100.0
                except Exception:
                    return 0.0

            by_cat: Dict[str, List] = defaultdict(list)
            for r in rows:
                m = dict(r)
                # Normalise prices to 0-1 range regardless of storage format
                ya = _norm_price(m.get("yes_ask") or m.get("last_price") or m.get("yes_bid"))
                if ya <= 0 or ya >= 1:
                    continue
                m["yes_ask"] = round(ya * 100, 2)   # store as cents for downstream compat
                m["no_ask"]  = round((1 - ya) * 100, 2)
                cat = _normalize_cat(m.get("category") or "general")
                by_cat[cat].append(m)

            result = []
            for cat, mlist in by_cat.items():
                mlist.sort(key=lambda m: m.get("volume", 0), reverse=True)
                for m in mlist[:max_per_cat]:
                    m["platform"]  = "kalshi"
                    m["_scan_cat"] = cat
                    result.append(m)

            logger.info(
                "Kalshi scan: %d markets across %d categories",
                len(result), len(by_cat),
            )
            return result
        except Exception as e:
            logger.warning("Kalshi category scan error: %s", e)
            return []

    async def scan_all_categories(
        self,
        max_per_tag: int = 5,
        max_total: int = 200,
        include_bulk: bool = True,
    ) -> List[Dict]:
        """
        Fetch markets from EVERY tag/sub-category on both platforms in parallel.

        Returns: flat list of market dicts sorted by _pre_score (best first),
                 deduplicated by ticker, diversity-balanced across categories.
        """
        n_tags = len(POLY_TAG_SLUGS)
        logger.info(
            "Starting full category scan: %d Poly tags + bulk fetch + Kalshi DB",
            n_tags,
        )

        # All tag fetches run concurrently — even if many return empty it's fast
        # Limit parallelism to avoid overwhelming the API (batch in groups of 20)
        all_tag_results: List[List[Dict]] = []
        batch_size = 20
        for i in range(0, len(POLY_TAG_SLUGS), batch_size):
            batch = POLY_TAG_SLUGS[i:i + batch_size]
            batch_results = await asyncio.gather(
                *[_fetch_poly_tag(tag, max_per_tag) for tag in batch],
                return_exceptions=True,
            )
            for r in batch_results:
                if isinstance(r, list):
                    all_tag_results.append(r)

        # Bulk fetch + Kalshi concurrently
        extras = []
        if include_bulk:
            extras.append(_fetch_poly_bulk(50))
        extras.append(self._kalshi_all_categories(max_per_tag))

        extra_results = await asyncio.gather(*extras, return_exceptions=True)

        # Merge and deduplicate
        seen_tickers: set = set()
        all_markets: List[Dict] = []

        for source in [*all_tag_results, *extra_results]:
            if not isinstance(source, list):
                continue
            for m in source:
                if not isinstance(m, dict):
                    continue
                ticker = m.get("ticker", "")
                title  = (m.get("title") or "").strip()
                if not ticker or ticker in seen_tickers:
                    continue
                if not title or len(title) < 5:
                    continue
                seen_tickers.add(ticker)
                m["_pre_score"] = _pre_score(m)
                all_markets.append(m)

        # Sort best first
        all_markets.sort(key=lambda m: m["_pre_score"], reverse=True)

        # Category-diverse top selection
        top = _diverse_top(all_markets, max_total, top_per_cat=4)

        cat_summary = dict(Counter(
            m.get("_scan_cat") or m.get("category") or "general"
            for m in top
        ).most_common(15))

        logger.info(
            "Category scan done: %d unique markets → top %d selected | cats: %s",
            len(all_markets), len(top),
            ", ".join(f"{k}:{v}" for k, v in list(cat_summary.items())[:8]),
        )
        return top

    async def scan_single_category(self, category: str, max_markets: int = 20) -> List[Dict]:
        """Scan one specific category + its sub-tags on both platforms."""
        # Find matching tag slugs
        matching_tags = [t for t in POLY_TAG_SLUGS if category.lower() in t][:5]
        if not matching_tags:
            matching_tags = [category]

        coros = [_fetch_poly_tag(tag, max_markets) for tag in matching_tags]
        results = await asyncio.gather(*coros, return_exceptions=True)

        merged: List[Dict] = []
        seen: set = set()
        for res in results:
            if isinstance(res, list):
                for m in res:
                    t = m.get("ticker", "")
                    if t and t not in seen:
                        merged.append(m)
                        seen.add(t)

        # Add matching Kalshi markets
        if self.db:
            try:
                rows = await self.db.fetchall(
                    "SELECT * FROM markets WHERE (status='open' OR status='') "
                    "AND (platform='kalshi' OR platform IS NULL) "
                    "AND yes_ask > 2 AND yes_ask < 98 "
                    "AND (LOWER(category) LIKE ? OR LOWER(title) LIKE ?) "
                    "ORDER BY volume DESC LIMIT ?",
                    (f"%{category}%", f"%{category}%", max_markets)
                )
                for r in (rows or []):
                    d = dict(r)
                    t = d.get("ticker", "")
                    if t and t not in seen:
                        d["_scan_cat"] = category
                        merged.append(d)
                        seen.add(t)
            except Exception as e:
                logger.debug("Kalshi category '%s' DB query error: %s", category, e)

        for m in merged:
            m["_pre_score"] = _pre_score(m)
        merged.sort(key=lambda m: m["_pre_score"], reverse=True)
        return merged


def summarize_by_category(markets: List[Dict]) -> Dict[str, int]:
    """Return count of markets per category — used in Discord reports."""
    cats = [
        _normalize_cat(m.get("_scan_cat") or m.get("category") or "general")
        for m in markets
    ]
    return dict(Counter(cats).most_common(12))
