"""
Web search context for live market decisions.

Aggregates headlines from multiple free sources in parallel:
  1. Google News RSS   — broad news coverage
  2. Yahoo News RSS    — additional coverage, often different stories
  3. Bing News RSS     — Microsoft news index, good for current events
  4. DuckDuckGo        — instant answer / background info (Wikipedia-backed)

No API keys required. All sources run in parallel with a shared timeout.
More sources = more context = higher AI confidence on current events.
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger("trading.web_search")

_TIMEOUT = httpx.Timeout(8.0)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _extract_query_terms(title: str) -> str:
    """Strip filler words from market title to produce a clean search query."""
    stopwords = {
        "will", "the", "a", "an", "to", "by", "at", "in", "on", "of",
        "or", "and", "is", "be", "for", "before", "after", "does", "when",
        "with", "from", "this", "that", "have", "are", "was", "its", "not",
        "what", "how", "who", "any", "all", "get", "set", "year", "month",
        "end", "win", "lose", "reach", "exceed", "above", "below", "new",
        "can", "could", "would", "should", "has", "had", "been", "were",
    }
    words = re.findall(r"[a-zA-Z0-9]{2,}", title)
    filtered = [w for w in words if w.lower() not in stopwords]
    return " ".join(filtered[:8])


def _parse_rss_items(xml_text: str, max_items: int = 5, strip_suffix: bool = False) -> List[str]:
    """Parse RSS XML and return up to max_items headline strings."""
    try:
        root = ET.fromstring(xml_text)
        headlines = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if strip_suffix:
                title = title.rsplit(" - ", 1)[0].strip()
            if title and len(title) > 10:
                headlines.append(title)
            if len(headlines) >= max_items:
                break
        return headlines
    except Exception:
        return []


async def _fetch_url(client: httpx.AsyncClient, url: str) -> str:
    """Fetch a URL and return response text, or empty string on failure."""
    try:
        r = await client.get(url)
        r.raise_for_status()
        return r.text
    except Exception:
        return ""


async def search_google_news(query: str, max_results: int = 5) -> List[str]:
    """Google News RSS — broad news coverage."""
    url = (
        f"https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        text = await _fetch_url(client, url)
    headlines = _parse_rss_items(text, max_results, strip_suffix=True)
    if headlines:
        logger.debug("Google News: %d headlines for '%s'", len(headlines), query[:40])
    return headlines


async def search_yahoo_news(query: str, max_results: int = 5) -> List[str]:
    """Yahoo News RSS — additional coverage."""
    url = f"https://news.yahoo.com/rss/search?p={quote_plus(query)}"
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        text = await _fetch_url(client, url)
    headlines = _parse_rss_items(text, max_results, strip_suffix=False)
    if headlines:
        logger.debug("Yahoo News: %d headlines for '%s'", len(headlines), query[:40])
    return headlines


async def search_bing_news(query: str, max_results: int = 5) -> List[str]:
    """Bing News RSS — Microsoft news index."""
    url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss"
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        text = await _fetch_url(client, url)
    headlines = _parse_rss_items(text, max_results, strip_suffix=False)
    if headlines:
        logger.debug("Bing News: %d headlines for '%s'", len(headlines), query[:40])
    return headlines


async def search_ddg_instant(query: str) -> Optional[str]:
    """DuckDuckGo instant answer — Wikipedia-backed background info."""
    try:
        url = (
            f"https://api.duckduckgo.com/?q={quote_plus(query)}"
            f"&format=json&no_html=1&skip_disambig=1"
        )
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()
        data = r.json()
        abstract = (data.get("AbstractText") or "").strip()
        if abstract and len(abstract) > 30:
            source = data.get("AbstractSource", "")
            return f"{abstract[:400]} (via {source})" if source else abstract[:400]
        for topic in (data.get("RelatedTopics") or [])[:2]:
            text = (topic.get("Text") or "").strip()
            if text and len(text) > 20:
                return text[:250]
        return None
    except Exception as e:
        logger.debug("DDG instant answer failed for '%s': %s", query[:50], e)
        return None


async def fetch_live_context(market_title: str, timeout: float = 10.0) -> str:
    """
    Fetch web search context for a live market from all sources in parallel.
    Returns a formatted block ready for AI prompt injection.
    """
    query = _extract_query_terms(market_title)
    if not query:
        return ""

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                search_google_news(query, max_results=5),
                search_yahoo_news(query, max_results=4),
                search_bing_news(query, max_results=4),
                search_ddg_instant(query),
                return_exceptions=True,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.debug("Web search timed out for: %s", query[:50])
        return ""

    google_h, yahoo_h, bing_h, ddg_abstract = results

    # Deduplicate headlines across all sources
    seen: set = set()
    all_headlines: List[Tuple[str, str]] = []  # (source, headline)
    for source, items in [
        ("Google", google_h if isinstance(google_h, list) else []),
        ("Yahoo",  yahoo_h  if isinstance(yahoo_h,  list) else []),
        ("Bing",   bing_h   if isinstance(bing_h,   list) else []),
    ]:
        for h in items:
            norm = h.lower()[:60]
            if norm not in seen:
                seen.add(norm)
                all_headlines.append((source, h))

    blocks = []

    if isinstance(ddg_abstract, str) and ddg_abstract:
        blocks.append(f"Background: {ddg_abstract}")

    if all_headlines:
        lines = [f"Recent news ({len(all_headlines)} headlines from Google/Yahoo/Bing):"]
        for i, (src, h) in enumerate(all_headlines[:12], 1):
            lines.append(f"  {i}. [{src}] {h}")
        blocks.append("\n".join(lines))

    if blocks:
        logger.info(
            "Web search: %d unique headlines + %s background for '%s'",
            len(all_headlines),
            "DDG abstract" if ddg_abstract else "no abstract",
            query[:50],
        )
        return "\n\n".join(blocks)

    logger.debug("Web search: no results for '%s'", query[:50])
    return ""
