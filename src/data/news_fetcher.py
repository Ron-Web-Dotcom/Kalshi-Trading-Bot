"""
Real-time news headlines via RSS feeds — no API key required.

Reads free public RSS feeds from AP, Reuters, BBC, and Politico.
Filters headlines relevant to a given market question by keyword overlap.
Falls back gracefully if any feed is unreachable.
"""

import logging
import asyncio
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.news_fetcher")

_TIMEOUT = httpx.Timeout(8.0)

# Free public RSS feeds — no auth needed
RSS_FEEDS: Dict[str, str] = {
    # General news — verified working
    "bbc_world":    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "bbc_biz":      "https://feeds.bbci.co.uk/news/business/rss.xml",
    "bbc_sport":    "https://feeds.bbci.co.uk/sport/rss.xml",
    "bbc_football": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "npr_top":      "https://feeds.npr.org/1001/rss.xml",
    "yahoo_news":   "https://news.yahoo.com/rss/",
    "google_news":  "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "aljaz":        "https://www.aljazeera.com/xml/rss/all.xml",
    # Finance / crypto
    "coindesk":     "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "bing_biz":     "https://www.bing.com/news/search?q=finance&format=rss",
    # Sports
    "espn_soccer":  "https://www.espn.com/espn/rss/soccer/news",
    "sky_sports":   "https://www.skysports.com/rss/12040",
}

# Category → feed names to query
CATEGORY_FEEDS: Dict[str, List[str]] = {
    "politics":    ["bbc_world", "npr_top", "yahoo_news", "google_news"],
    "economics":   ["bbc_biz", "coindesk", "bing_biz", "yahoo_news"],
    "finance":     ["bbc_biz", "coindesk", "bing_biz"],
    "crypto":      ["coindesk", "bbc_biz", "yahoo_news"],
    "sports":      ["bbc_sport", "espn_soccer", "sky_sports"],
    "soccer":      ["bbc_football", "espn_soccer", "sky_sports"],
    "technology":  ["bbc_world", "google_news", "yahoo_news"],
    "health":      ["bbc_world", "npr_top"],
    "weather":     ["bbc_world", "npr_top"],
    "default":     ["bbc_world", "yahoo_news", "google_news"],
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
        logger.warning("RSS feed %s failed: %s", name, e)
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
    """Metaculus removed (403 Forbidden) — returns None immediately."""
    return None


def format_headlines(headlines: List[str]) -> str:
    """Format headlines as a compact block for the AI prompt."""
    if not headlines:
        return ""
    lines = ["Recent relevant headlines:"]
    for i, h in enumerate(headlines, 1):
        lines.append(f"  {i}. {h}")
    return "\n".join(lines)
