"""
Real-time news headlines via RSS feeds — no API key required.

Reads free public RSS feeds from AP, Reuters, BBC, and Politico.
Filters headlines relevant to a given market question by keyword overlap.
Falls back gracefully if any feed is unreachable.
"""

import logging
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.news_fetcher")

_TIMEOUT = httpx.Timeout(8.0)

# Free public RSS feeds — no auth needed
RSS_FEEDS: Dict[str, str] = {
    "ap_top":       "https://feeds.apnews.com/rss/apf-topnews",
    "ap_politics":  "https://feeds.apnews.com/rss/apf-politics",
    "ap_finance":   "https://feeds.apnews.com/rss/apf-finance",
    "ap_sports":    "https://feeds.apnews.com/rss/apf-sports",
    "ap_tech":      "https://feeds.apnews.com/rss/apf-technology",
    "reuters_top":  "https://feeds.reuters.com/reuters/topNews",
    "reuters_biz":  "https://feeds.reuters.com/reuters/businessNews",
    "bbc_world":    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "bbc_biz":      "https://feeds.bbci.co.uk/news/business/rss.xml",
    "bbc_sport":    "https://feeds.bbci.co.uk/sport/rss.xml",
    "bbc_football": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "politico":     "https://www.politico.com/rss/politicopicks.xml",
    "coindesk":     "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "wsj_markets":  "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    # Soccer-specific feeds
    "the_athletic_soccer": "https://theathletic.com/rss-feed/soccer/",
    "espn_soccer":  "https://www.espn.com/espn/rss/soccer/news",
    "sky_sports":   "https://www.skysports.com/rss/12040",       # Sky Sports football
    "goal_com":     "https://www.goal.com/feeds/en/news",
}

# Category → feed names to query
CATEGORY_FEEDS: Dict[str, List[str]] = {
    "politics":    ["ap_politics", "politico", "bbc_world", "reuters_top"],
    "economics":   ["ap_finance", "reuters_biz", "wsj_markets", "bbc_biz"],
    "finance":     ["ap_finance", "reuters_biz", "wsj_markets"],
    "crypto":      ["coindesk", "ap_finance", "reuters_biz"],
    "sports":      ["ap_sports", "bbc_sport", "espn_soccer"],
    "soccer":      ["bbc_football", "espn_soccer", "sky_sports", "goal_com", "ap_sports"],
    "technology":  ["ap_tech", "reuters_top"],
    "health":      ["ap_top", "bbc_world"],
    "weather":     ["ap_top"],
    "default":     ["ap_top", "reuters_top"],
}


async def _fetch_feed(client: httpx.AsyncClient, name: str, url: str) -> List[Dict]:
    """Fetch and parse one RSS feed. Returns list of {title, link, published}."""
    try:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.text)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}

        items = []
        for item in root.iter("item"):
            title  = (item.findtext("title") or "").strip()
            link   = (item.findtext("link")  or "").strip()
            pubdate = item.findtext("pubDate") or ""
            if title:
                items.append({
                    "title":     title,
                    "link":      link,
                    "published": pubdate,
                    "feed":      name,
                })
        return items[:20]   # cap per feed
    except Exception as e:
        logger.debug("RSS feed %s failed: %s", name, e)
        return []


def _score_relevance(headline: str, keywords: List[str]) -> int:
    """Count keyword matches in headline (case-insensitive)."""
    h = headline.lower()
    return sum(1 for kw in keywords if kw.lower() in h)


async def fetch_headlines(
    keywords: List[str],
    category: str = "default",
    max_headlines: int = 6,
) -> List[str]:
    """
    Fetch relevant headlines for a market.

    Args:
        keywords : words from the market title (e.g. ["bitcoin", "40k", "year-end"])
        category : Kalshi market category string
        max_headlines : how many to return

    Returns:
        List of plain-text headline strings, sorted by relevance.
    """
    feed_names = CATEGORY_FEEDS.get(category.lower(), CATEGORY_FEEDS["default"])

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        tasks = [
            _fetch_feed(client, name, RSS_FEEDS[name])
            for name in feed_names
            if name in RSS_FEEDS
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Flatten
    all_items: List[Dict] = []
    for r in results:
        if isinstance(r, list):
            all_items.extend(r)

    if not all_items:
        return []

    # Deduplicate by title
    seen: set = set()
    unique = []
    for item in all_items:
        t = item["title"]
        if t not in seen:
            seen.add(t)
            unique.append(item)

    # Score by keyword relevance, fallback to recency (keep top N of each)
    scored = [(item, _score_relevance(item["title"], keywords)) for item in unique]
    scored.sort(key=lambda x: x[1], reverse=True)

    top = scored[:max_headlines]
    return [item["title"] for item, _ in top]


async def fetch_community_prediction(market_title: str) -> Optional[str]:
    """
    Check Metaculus for community prediction on similar questions.
    Returns a one-line summary or None.
    """
    try:
        url = "https://www.metaculus.com/api2/questions/"
        params = {
            "search":   market_title[:80],
            "status":   "open",
            "order_by": "-activity",
            "limit":    3,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            results = r.json().get("results", [])

        lines = []
        for q in results[:2]:
            title = (q.get("title") or "")[:70]
            cp    = q.get("community_prediction", {})
            pred  = cp.get("full", {}).get("q2") if cp else None
            if pred is not None:
                lines.append(f"Metaculus: '{title}' → {pred*100:.0f}% community estimate")
        return "\n".join(lines) if lines else None
    except Exception as e:
        logger.debug("Metaculus fetch failed: %s", e)
        return None


def format_headlines(headlines: List[str]) -> str:
    """Format headlines as a compact block for the AI prompt."""
    if not headlines:
        return ""
    lines = ["Recent relevant headlines:"]
    for i, h in enumerate(headlines, 1):
        lines.append(f"  {i}. {h}")
    return "\n".join(lines)
