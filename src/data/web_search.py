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
  12. Polymarket Gamma API — independent prediction market YES/NO prices
  13. PredictIt API        — US politics contracts (elections, approval, legislation)

  KNOWLEDGE / BACKGROUND
  14. Wikipedia REST API   — authoritative article summaries for key entities
  15. DuckDuckGo Instant   — Wikipedia-backed instant answers
  16. Wikidata API         — structured facts (dates, relationships, numbers)

  COMMUNITY SENTIMENT
  17. Reddit RSS search    — community discussion and sentiment

  VIDEO COVERAGE
  18. YouTube search       — video titles, channels, view counts (no API key)
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
        logger.warning("GET failed [%s]: %s", url[:80], e)
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
        logger.warning("Manifold failed: %s", e)
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
        logger.warning("Metaculus failed: %s", e)
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
        logger.warning("Wikipedia failed for '%s': %s", entity, e)
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
        logger.warning("DDG failed: %s", e)
        return None


async def _wikidata(q: str) -> Optional[str]:
    """
    Wikidata API — structured facts: dates, numeric values, relationships.
    Good for resolving entity facts like birth dates, country populations,
    GDP figures, team rosters, election dates.
    """
    try:
        data = await _get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbsearchentities",
                "search": q[:80],
                "language": "en",
                "limit": 3,
                "format": "json",
            },
            as_json=True,
        )
        if not data:
            return None
        items = data.get("search", [])
        lines = []
        for item in items[:3]:
            label = item.get("label", "")
            desc  = item.get("description", "")
            if label and desc:
                lines.append(f"Wikidata: {label} — {desc}")
        return "\n".join(lines) if lines else None
    except Exception as e:
        logger.warning("Wikidata failed: %s", e)
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
        logger.warning("Reddit search failed: %s", e)
        return []


async def _youtube_deep(q: str, timeout: float) -> Optional[str]:
    """Kick off deep YouTube research — transcripts + descriptions."""
    try:
        from src.data.youtube_research import deep_youtube_research
        return await deep_youtube_research(q, timeout=min(timeout - 2, 16.0))
    except Exception as e:
        logger.warning("YouTube deep research failed: %s", e)
        return None


async def _youtube_search(q: str) -> List[str]:
    """
    YouTube video search — scrapes titles, channels, and view counts
    from youtube.com/results without requiring an API key.

    Returns list of strings like:
      'YouTube [ChannelName]: "Video Title" (1.2M views)'
    """
    try:
        url  = f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp=CAI%253D"  # sorted by upload date
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as _yc:
                _yr = await _yc.get(url, headers={**_HEADERS, "Accept-Language": "en-US,en;q=0.9"})
                html = _yr.text
        except Exception:
            return []
        if not html or not isinstance(html, str):
            return []

        videos = []

        # YouTube embeds initial data as a JSON blob in a <script> tag
        prefix = 'var ytInitialData\s*=\s*'
        idx = html.find('var ytInitialData = ')
        if idx == -1:
            idx = html.find('var ytInitialData=')
        if idx != -1:
            # Advance past the '= ' to the opening brace
            brace_start = html.index('{', idx)
            # Walk the string counting braces to find the matching close
            depth = 0
            end = brace_start
            for i, ch in enumerate(html[brace_start:], brace_start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            raw_json = html[brace_start:end]
            match = True  # sentinel so the block below runs
        else:
            raw_json = None
            match = False
        if match and raw_json:
            import json
            try:
                yt_data = json.loads(raw_json)
                # Navigate to video renderers
                contents = (
                    yt_data
                    .get("contents", {})
                    .get("twoColumnSearchResultsRenderer", {})
                    .get("primaryContents", {})
                    .get("sectionListRenderer", {})
                    .get("contents", [])
                )
                for section in contents:
                    items = (
                        section
                        .get("itemSectionRenderer", {})
                        .get("contents", [])
                    )
                    for item in items:
                        vr = item.get("videoRenderer", {})
                        if not vr:
                            continue
                        title   = "".join(
                            r.get("text", "")
                            for r in vr.get("title", {}).get("runs", [])
                        ).strip()
                        channel = "".join(
                            r.get("text", "")
                            for r in (
                                vr.get("ownerText", {}).get("runs", [])
                                or vr.get("longBylineText", {}).get("runs", [])
                            )
                        ).strip()
                        views   = (
                            vr.get("viewCountText", {}).get("simpleText", "")
                            or vr.get("shortViewCountText", {}).get("simpleText", "")
                        ).strip()
                        pub     = vr.get("publishedTimeText", {}).get("simpleText", "").strip()

                        if title and len(title) > 8:
                            parts = [f'"{title}"']
                            if channel:
                                parts.insert(0, f"[{channel}]")
                            if views:
                                parts.append(f"({views})")
                            if pub:
                                parts.append(pub)
                            videos.append("YouTube " + " ".join(parts))
                        if len(videos) >= 6:
                            break
                    if len(videos) >= 6:
                        break
            except Exception:
                pass

        # Fallback: regex title scrape if JSON parse failed
        if not videos:
            for m in re.finditer(r'"title":\{"runs":\[\{"text":"([^"]{8,120})"', html):
                t = m.group(1).strip()
                if t and t not in ("YouTube", "Shorts") and not t.startswith("http"):
                    videos.append(f'YouTube: "{t}"')
                if len(videos) >= 5:
                    break

        return videos

    except Exception as e:
        logger.warning("YouTube search failed: %s", e)
        return []


# ── Prediction market price fetchers ─────────────────────────────────────────

def _kw_overlap(a: str, b: str) -> float:
    """Jaccard similarity between two strings, ignoring stopwords."""
    sw = {"will", "the", "a", "an", "in", "on", "at", "to", "of", "by",
          "be", "is", "or", "and", "for", "it", "its", "this", "does"}
    wa = {w for w in re.sub(r"[^\w\s]", " ", a.lower()).split() if len(w) > 2 and w not in sw}
    wb = {w for w in re.sub(r"[^\w\s]", " ", b.lower()).split() if len(w) > 2 and w not in sw}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


async def _polymarket_price(title: str) -> Optional[str]:
    """
    Search Polymarket Gamma API for a market matching the title.
    Returns a formatted context block with YES/NO prices, or None.
    """
    try:
        q = " ".join(title.split()[:7])
        data = await _get(
            "https://gamma-api.polymarket.com/markets",
            params={"search": q, "active": "true", "closed": "false", "limit": 8},
            as_json=True,
        )
        if not data:
            return None
        raw = data if isinstance(data, list) else data.get("data", [])
        best, best_score = None, 0.0
        for m in raw:
            score = _kw_overlap(title, m.get("question", ""))
            if score > best_score:
                best, best_score = m, score
        if not best or best_score < 0.15:
            return None
        prices = best.get("outcomePrices") or []
        if len(prices) < 2:
            return None
        yes_p = float(prices[0]) * 100
        no_p  = float(prices[1]) * 100
        vol   = float(best.get("volume") or 0)
        return (
            f"Polymarket: \"{best.get('question','')}\"\n"
            f"  YES: {yes_p:.0f}%  |  NO: {no_p:.0f}%  |  Volume: ${vol:,.0f}\n"
            f"  (independent prediction market — gaps vs Kalshi signal mispricing)"
        )
    except Exception as e:
        logger.warning("Polymarket price fetch failed: %s", e)
        return None


async def _predictit_price(title: str) -> Optional[str]:
    """
    Search PredictIt public API for a US politics market matching the title.
    Returns a formatted context block, or None.
    PredictIt is most useful for elections, approval ratings, legislation.
    """
    # Only worth calling for politics-flavoured titles
    _POL_WORDS = {"president", "congress", "senate", "house", "elect", "vote",
                  "democrat", "republican", "trump", "biden", "harris", "governor",
                  "approval", "bill", "law", "scotus", "supreme", "court", "fed",
                  "rate", "fomc", "inflation", "cpi", "gdp", "unemployment"}
    title_lower = title.lower()
    if not any(w in title_lower for w in _POL_WORDS):
        return None
    try:
        data = await _get(
            "https://www.predictit.org/api/marketdata/all/",
            as_json=True,
        )
        if not data:
            return None
        markets = data.get("markets", [])
        best, best_score = None, 0.0
        for m in markets:
            score = _kw_overlap(title, m.get("name", ""))
            if score > best_score:
                best, best_score = m, score
        if not best or best_score < 0.18:
            return None
        contracts = best.get("contracts", [])
        if not contracts:
            return None
        lines = [f"PredictIt: \"{best.get('name','')[:100]}\""]
        for c in contracts[:4]:
            yes_p = (c.get("lastTradePrice") or c.get("bestYesPrice") or 0) * 100
            lines.append(f"  {c.get('name','')[:60]}: {yes_p:.0f}%")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("PredictIt fetch failed: %s", e)
        return None


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
                _npr_news(q),
                _ap_news(q),
                _reuters_news(q),
                _bbc_news(q),
                # Prediction market cross-reference (4 independent sources)
                _manifold_markets(q),
                _metaculus(q),
                _polymarket_price(market_title),
                _predictit_price(market_title),
                # Knowledge / background
                _ddg_instant(q),
                _wikidata(q),
                *wiki_coros,
                # Community + video titles (fast)
                _reddit_search(q),
                _youtube_search(q),
                # YouTube deep research — transcripts + descriptions (slower, high value)
                _youtube_deep(q, timeout),
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
    npr_h      = _next() or []
    ap_h       = _next() or []
    reuters_h  = _next() or []
    bbc_h      = _next() or []
    manifold   = _next()
    metaculus  = _next()
    poly_price = _next()
    predictit  = _next()
    ddg        = _next()
    wikidata   = _next()
    wiki_results = [_next() for _ in wiki_coros]
    reddit_h      = _next() or []
    youtube_h     = _next() or []
    youtube_deep  = _next()   # full transcript/description block or None

    # Deduplicate and combine all headlines
    seen: set = set()
    all_headlines: List[Tuple[str, str]] = []
    for source, items in [
        ("Google",   google_h),
        ("Yahoo",    yahoo_h),
        ("Bing",     bing_h),
        ("Guardian", guardian_h),
        ("AlJazeera",alj_h),
        ("NPR",      npr_h),
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

    # 2b. Wikidata structured facts
    if isinstance(wikidata, str) and wikidata:
        blocks.append(f"=== WIKIDATA FACTS ===\n{wikidata}")

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

    # 5. YouTube — deep research (transcripts + descriptions) takes priority
    if isinstance(youtube_deep, str) and youtube_deep:
        blocks.append(youtube_deep)
    elif youtube_h:
        # Fallback: title-only if deep research timed out
        lines = ["=== VIDEO COVERAGE (YouTube — titles only) ==="]
        for vid in youtube_h[:6]:
            lines.append(f"  • {vid}")
        blocks.append("\n".join(lines))

    # 6. Prediction market cross-references (Manifold, Metaculus, Polymarket, PredictIt)
    pm_lines = []
    if isinstance(manifold, str) and manifold:
        pm_lines.append(manifold)
    if isinstance(metaculus, str) and metaculus:
        pm_lines.append(metaculus)
    if isinstance(poly_price, str) and poly_price:
        pm_lines.append(poly_price)
    if isinstance(predictit, str) and predictit:
        pm_lines.append(predictit)
    if pm_lines:
        blocks.append("=== OTHER PREDICTION MARKETS ===\n" + "\n".join(pm_lines))

    if blocks:
        total_sources = sum([
            bool(all_headlines), bool(wiki_text), bool(ddg),
            bool(reddit_h), bool(youtube_deep or youtube_h), bool(pm_lines),
        ])
        def _hit(v) -> str:
            return "✅" if v else "❌"

        pm_sources = "+".join(filter(None, [
            "Manifold"   if isinstance(manifold,   str) and manifold   else "",
            "Metaculus"  if isinstance(metaculus,  str) and metaculus  else "",
            "Polymarket" if isinstance(poly_price, str) and poly_price else "",
            "PredictIt"  if isinstance(predictit,  str) and predictit  else "",
        ])) or "none"

        logger.info(
            "Context sources for '%s':\n"
            "  News:       %s Google  %s Yahoo  %s Bing  %s Guardian  %s AlJazeera  %s NPR  %s AP  %s Reuters  %s BBC\n"
            "  Knowledge:  %s Wikipedia(%d)  %s DDG  %s Wikidata  %s Reddit  %s YouTube\n"
            "  Pred mkts:  %s Manifold  %s Metaculus  %s Polymarket  %s PredictIt\n"
            "  Total: %d headlines | %d wiki blocks | pred_markets=%s",
            q[:60],
            _hit(google_h), _hit(yahoo_h), _hit(bing_h), _hit(guardian_h),
            _hit(alj_h),    _hit(npr_h),  _hit(ap_h),   _hit(reuters_h), _hit(bbc_h),
            _hit(wiki_text), len(wiki_text), _hit(ddg), _hit(wikidata), _hit(reddit_h), _hit(youtube_deep or youtube_h),
            _hit(manifold),  _hit(metaculus), _hit(poly_price), _hit(predictit),
            len(all_headlines), len(wiki_text), pm_sources,
        )
        return "\n\n".join(blocks)

    logger.debug("Web search: no results for '%s'", q[:60])
    return ""
