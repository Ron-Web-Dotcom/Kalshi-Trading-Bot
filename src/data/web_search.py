"""
Web search context for live market decisions.

Uses Google News RSS (free, no API key) to fetch recent headlines
about any market question. Falls back to DuckDuckGo instant answers.
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Optional
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger("trading.web_search")

_TIMEOUT = httpx.Timeout(8.0)
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"}


def _extract_query_terms(title: str) -> str:
    """Strip filler words from market title to make a good search query."""
    stopwords = {
        "will", "the", "a", "an", "to", "by", "at", "in", "on", "of",
        "or", "and", "is", "be", "for", "before", "after", "does", "when",
        "with", "from", "this", "that", "have", "are", "was", "its", "not",
        "what", "how", "who", "any", "all", "get", "set", "year", "month",
        "end", "win", "lose", "reach", "exceed", "above", "below", "new",
    }
    words = re.findall(r"[a-zA-Z0-9]{2,}", title)
    filtered = [w for w in words if w.lower() not in stopwords]
    return " ".join(filtered[:8])


async def search_google_news(query: str, max_results: int = 5) -> List[str]:
    """Fetch recent headlines from Google News RSS for a query."""
    try:
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()
        root = ET.fromstring(r.text)
        headlines = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            # Google News titles include source like "Title - Source"
            title = title.rsplit(" - ", 1)[0].strip()
            if title and len(title) > 10:
                headlines.append(title)
            if len(headlines) >= max_results:
                break
        return headlines
    except Exception as e:
        logger.debug("Google News search failed for '%s': %s", query[:50], e)
        return []


async def search_ddg_instant(query: str) -> Optional[str]:
    """DuckDuckGo instant answer — returns abstract text if available."""
    try:
        url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()
        data = r.json()
        abstract = (data.get("AbstractText") or "").strip()
        if abstract and len(abstract) > 30:
            source = data.get("AbstractSource", "")
            return f"{abstract[:300]} (via {source})" if source else abstract[:300]
        # Try related topics
        for topic in (data.get("RelatedTopics") or [])[:2]:
            text = (topic.get("Text") or "").strip()
            if text and len(text) > 20:
                return text[:200]
        return None
    except Exception as e:
        logger.debug("DDG instant answer failed for '%s': %s", query[:50], e)
        return None


async def fetch_live_context(market_title: str, timeout: float = 8.0) -> str:
    """
    Fetch web search context for a live market.
    Runs Google News + DDG in parallel, returns formatted block.
    """
    query = _extract_query_terms(market_title)
    if not query:
        return ""

    try:
        headlines, abstract = await asyncio.wait_for(
            asyncio.gather(
                search_google_news(query, max_results=5),
                search_ddg_instant(query),
                return_exceptions=True,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.debug("Web search timed out for: %s", query[:50])
        return ""

    blocks = []

    if isinstance(abstract, str) and abstract:
        blocks.append(f"Background: {abstract}")

    if isinstance(headlines, list) and headlines:
        lines = ["Recent news:"]
        for i, h in enumerate(headlines, 1):
            lines.append(f"  {i}. {h}")
        blocks.append("\n".join(lines))

    if blocks:
        logger.debug("Web search found %d blocks for: %s", len(blocks), query[:50])
        return "\n\n".join(blocks)

    return ""
