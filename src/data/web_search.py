"""
Web search & knowledge aggregator for live market decisions.

Runs ALL sources in parallel with a shared timeout — more sources =
richer AI context = higher confidence on current events.

Sources (all free, no API key required):
  NEWS SEARCH
  1.  Google News RSS     — broad, real-time news index
  2.  Yahoo News RSS      — additional coverage, different editorial angle
  3.  Bing News RSS       — Microsoft news index, strong for breaking news
  4.  The Guardian RSS    — quality international journalism
  5.  Al Jazeera RSS      — international / non-Western perspective
  6.  NPR News RSS        — US public radio
  7.  AP News RSS         — wire service, authoritative
  8.  Reuters RSS         — wire service, financial/political
  9.  BBC News RSS        — UK/world perspective

  PREDICTION MARKETS (cross-reference)
  10. Manifold Markets API — community forecasts on same or similar questions
  11. Metaculus API        — aggregated community probability estimates

  KNOWLEDGE / BACKGROUND
  12. Wikipedia REST API   — authoritative article summaries for key entities
  13. DuckDuckGo Instant   — Wikipedia-backed instant answers
  14. Wikidata API         — structured facts (dates, relationships, numbers)

  COMMUNITY SENTIMENT
  15. Reddit RSS search    — community discussion and sentiment
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus

import httpx

logger = logging.getLogger("trading.web_search")

_TIMEOUT  = httpx.Timeout(9.0)
_HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, application/json, text/xml, */*",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

_STOPWORDS = {
    "will", "the", "a", "an", "to", "by", "at", "in", "on", "of", "or",
    "and", "is", "be", "for", "before", "after", "does", "when", "with",
    "from", "this", "that", "have", "are", "was", "its", "not", "what",
    "how", "who", "any", "all", "get", "set", "year", "month", "end",
    "win", "lose", "reach", "exceed", "above", "below", "new", "can",
    "could", "would", "should", "has", "had", "been", "were", "than",
    "more", "less", "over", "under", "into", "out", "up", "down", "do",
}


def _query(title: str, n: int = 8) -> str:
    """Extract meaningful search terms from a market title."""
    words = re.findall(r"[a-zA-Z0-9]{2,}", title)
    return " ".join(w for w in words if w.lower() not in _STOPWORDS)[:n * 12]


def _extract_entities(title: str) -> List[str]:
    """Extract capitalised entity names (people, places, orgs) from a title."""
    return re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", title)[:3]


async def _get(url: str, params: dict = None, as_json: bool = False):
    """Single async GET — returns text or dict, None on failure."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS,
                                     follow_redirects=True) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json() if as_json else r.text
    except Exception as e:
        logger.debug("GET %s failed: %s", url[:80], e)
        return None


def _rss_headlines(xml_text: str, max_items: int = 5,
                   strip_source: bool = False) -> List[str]:
    """Parse RSS XML → list of headline strings."""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
        out = []
        for item in root.iter("item"):
            t = (item.findtext("title") or "").strip()
            if strip_source:
                t = t.rsplit(" - ", 1)[0].strip()
            if t and len(t) > 10:
                out.append(t)
            if len(out) >= max_items:
                break
        return out
    except Exception:
        return []


# ── News search (RSS) ─────────────────────────────────────────────────────────

async def _google_news(q: str)  -> List[str]:
    t = await _get(f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en")
    return _rss_headlines(t, 6, strip_source=True)

async def _yahoo_news(q: str)   -> List[str]:
    t = await _get(f"https://news.yahoo.com/rss/search?p={quote_plus(q)}")
    return _rss_headlines(t, 5)

async def _bing_news(q: str)    -> List[str]:
    t = await _get(f"https://www.bing.com/news/search?q={quote_plus(q)}&format=rss")
    return _rss_headlines(t, 5)

async def _guardian_news(q: str) -> List[str]:
    t = await _get(f"https://www.theguardian.com/search?q={quote_plus(q)}&format=rss")
    return _rss_headlines(t, 4)

async def _aljazeera_news(q: str) -> List[str]:
    t = await _get(f"https://www.aljazeera.com/search/{quote_plus(q)}?format=rss")
    return _rss_headlines(t, 3)

async def _npr_news(q: str) -> List[str]:
    # NPR doesn't have query-based RSS, use topic feed for politics/general
    t = await _get("https://feeds.npr.org/1001/rss.xml")
    return _rss_headlines(t, 3)

async def _ap_news(q: str) -> List[str]:
    t = await _get("https://feeds.apnews.com/rss/apf-topnews")
    headlines = _rss_headlines(t, 10)
    # filter to relevant ones
    kws = q.lower().split()
    return [h for h in headlines if any(k in h.lower() for k in kws)][:4]

async def _reuters_news(q: str) -> List[str]:
    t = await _get("https://feeds.reuters.com/reuters/topNews")
    headlines = _rss_headlines(t, 10)
    kws = q.lower().split()
    return [h for h in headlines if any(k in h.lower() for k in kws)][:4]

async def _bbc_news(q: str) -> List[str]:
    t = await _get("https://feeds.bbci.co.uk/news/rss.xml")
    headlines = _rss_headlines(t, 10)
    kws = q.lower().split()
    return [h for h in headlines if any(k in h.lower() for k in kws)][:4]


# ── Prediction market cross-reference ────────────────────────────────────────

async def _manifold_markets(q: str) -> Optional[str]:
    """Manifold Markets — community probability on similar questions."""
    try:
        data = await _get(
            "https://api.manifold.markets/v0/search-markets",
            params={"term": q[:80], "limit": 3, "sort": "liquidity"},
            as_json=True,
        )
        if not data:
            return None
        lines = []
        for m in (data if isinstance(data, list) else [])[:3]:
            title = (m.get("question") or "")[:70]
            prob  = m.get("probability")
            vol   = m.get("volume", 0) or 0
            if prob is not None and title:
                lines.append(
                    f"Manifold: '{title}' → {prob*100:.0f}% YES "
                    f"(vol: ${vol:,.0f})"
                )
        return "\n".join(lines) if lines else None
    except Exception as e:
        logger.debug("Manifold failed: %s", e)
        return None


async def _metaculus(q: str) -> Optional[str]:
    """Metaculus community prediction aggregates."""
    try:
        data = await _get(
            "https://www.metaculus.com/api2/questions/",
            params={"search": q[:80], "status": "open",
                    "order_by": "-activity", "limit": 3},
            as_json=True,
        )
        if not data:
            return None
        lines = []
        for item in (data.get("results") or [])[:2]:
            title = (item.get("title") or "")[:70]
            cp    = item.get("community_prediction") or {}
            pred  = (cp.get("full") or {}).get("q2")
            if pred is not None and title:
                lines.append(
                    f"Metaculus community: '{title}' → {pred*100:.0f}%"
                )
        return "\n".join(lines) if lines else None
    except Exception as e:
        logger.debug("Metaculus failed: %s", e)
        return None


# ── Knowledge / background ────────────────────────────────────────────────────

async def _wikipedia(entity: str) -> Optional[str]:
    """Wikipedia REST API — article summary for a named entity."""
    try:
        slug = entity.strip().replace(" ", "_")
        data = await _get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(slug)}",
            as_json=True,
        )
        if not data or data.get("type") == "disambiguation":
            return None
        extract = (data.get("extract") or "").strip()
        title   = data.get("title", entity)
        if extract and len(extract) > 40:
            return f"Wikipedia — {title}: {extract[:400]}"
        return None
    except Exception as e:
        logger.debug("Wikipedia failed for '%s': %s", entity, e)
        return None


async def _ddg_instant(q: str) -> Optional[str]:
    """DuckDuckGo instant answer — quick background, Wikipedia-backed."""
    try:
        data = await _get(
            f"https://api.duckduckgo.com/?q={quote_plus(q)}"
            f"&format=json&no_html=1&skip_disambig=1",
            as_json=True,
        )
        if not data:
            return None
        abstract = (data.get("AbstractText") or "").strip()
        if abstract and len(abstract) > 30:
            src = data.get("AbstractSource", "")
            return f"{abstract[:400]}{f' (via {src})' if src else ''}"
        for topic in (data.get("RelatedTopics") or [])[:2]:
            text = (topic.get("Text") or "").strip()
            if text and len(text) > 20:
                return text[:250]
        return None
    except Exception as e:
        logger.debug("DDG failed: %s", e)
        return None


async def _reddit_search(q: str) -> List[str]:
    """Reddit JSON search — top posts about the query."""
    try:
        data = await _get(
            f"https://www.reddit.com/search.json?q={quote_plus(q)}&sort=new&limit=5&t=week",
            as_json=True,
        )
        if not data:
            return []
        posts = []
        for child in (data.get("data", {}).get("children") or [])[:5]:
            post  = child.get("data", {})
            title = (post.get("title") or "").strip()
            score = post.get("score", 0) or 0
            sub   = post.get("subreddit", "")
            if title and len(title) > 10:
                posts.append(f"r/{sub} ({score:,} upvotes): {title}")
        return posts
    except Exception as e:
        logger.debug("Reddit search failed: %s", e)
        return []


# ── Main aggregator ───────────────────────────────────────────────────────────

async def fetch_live_context(market_title: str, timeout: float = 12.0) -> str:
    """
    Aggregate context from ALL knowledge sources in parallel.
    Returns a rich, formatted block for AI prompt injection.
    """
    q       = _query(market_title)
    entities = _extract_entities(market_title)

    if not q:
        return ""

    # Build all coroutines — wiki lookups for each entity name
    wiki_coros = [_wikipedia(e) for e in entities[:2]]

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                # News search (9 sources)
                _google_news(q),
                _yahoo_news(q),
                _bing_news(q),
                _guardian_news(q),
                _aljazeera_news(q),
                _ap_news(q),
                _reuters_news(q),
                _bbc_news(q),
                # Prediction market cross-reference
                _manifold_markets(q),
                _metaculus(q),
                # Knowledge / background
                _ddg_instant(q),
                *wiki_coros,
                # Community
                _reddit_search(q),
                return_exceptions=True,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("Web search timed out after %.0fs for: %s", timeout, q[:60])
        return ""

    # Unpack results
    idx = 0
    def _next():
        nonlocal idx
        v = results[idx]; idx += 1
        return v if not isinstance(v, Exception) else None

    google_h   = _next() or []
    yahoo_h    = _next() or []
    bing_h     = _next() or []
    guardian_h = _next() or []
    alj_h      = _next() or []
    ap_h       = _next() or []
    reuters_h  = _next() or []
    bbc_h      = _next() or []
    manifold   = _next()
    metaculus  = _next()
    ddg        = _next()
    wiki_results = [_next() for _ in wiki_coros]
    reddit_h   = _next() or []

    # Deduplicate and combine all headlines
    seen: set = set()
    all_headlines: List[Tuple[str, str]] = []
    for source, items in [
        ("Google",   google_h),
        ("Yahoo",    yahoo_h),
        ("Bing",     bing_h),
        ("Guardian", guardian_h),
        ("AlJazeera",alj_h),
        ("AP",       ap_h),
        ("Reuters",  reuters_h),
        ("BBC",      bbc_h),
    ]:
        for h in (items or []):
            norm = re.sub(r"\s+", " ", h.lower()[:70])
            if norm not in seen:
                seen.add(norm)
                all_headlines.append((source, h))

    blocks = []

    # 1. Wikipedia background for named entities
    wiki_text = [w for w in wiki_results if isinstance(w, str) and w]
    if wiki_text:
        blocks.append("=== BACKGROUND (Wikipedia) ===\n" + "\n\n".join(wiki_text))

    # 2. DuckDuckGo instant answer
    if isinstance(ddg, str) and ddg:
        blocks.append(f"Background (DDG): {ddg}")

    # 3. News headlines from all sources
    if all_headlines:
        src_counts: Dict[str, int] = {}
        for src, _ in all_headlines:
            src_counts[src] = src_counts.get(src, 0) + 1
        src_summary = ", ".join(f"{s}:{n}" for s, n in src_counts.items())
        lines = [
            f"=== RECENT NEWS ({len(all_headlines)} unique headlines — {src_summary}) ==="
        ]
        for i, (src, h) in enumerate(all_headlines[:20], 1):
            lines.append(f"  {i:2}. [{src}] {h}")
        blocks.append("\n".join(lines))

    # 4. Reddit community sentiment
    if reddit_h:
        lines = ["=== COMMUNITY DISCUSSION (Reddit) ==="]
        for post in reddit_h[:5]:
            lines.append(f"  • {post}")
        blocks.append("\n".join(lines))

    # 5. Prediction market cross-references
    pm_lines = []
    if isinstance(manifold, str) and manifold:
        pm_lines.append(manifold)
    if isinstance(metaculus, str) and metaculus:
        pm_lines.append(metaculus)
    if pm_lines:
        blocks.append("=== OTHER PREDICTION MARKETS ===\n" + "\n".join(pm_lines))

    if blocks:
        total_sources = sum([
            bool(all_headlines), bool(wiki_text), bool(ddg),
            bool(reddit_h), bool(pm_lines),
        ])
        logger.info(
            "Web search: %d headlines | %d wiki | reddit=%s | pred_markets=%s | query='%s'",
            len(all_headlines), len(wiki_text),
            "yes" if reddit_h else "no",
            "yes" if pm_lines else "no",
            q[:60],
        )
        return "\n\n".join(blocks)

    logger.debug("Web search: no results for '%s'", q[:60])
    return ""
