"""
Live Event Detector — answers ONE question per market:
"Is the real-world event behind this prediction happening RIGHT NOW?"

Used exclusively by the BOT ALERT system. A market only gets a BOT ALERT
when the underlying event is genuinely live — game in progress, crypto
moving hard, election night, Fed decision day, storm active, etc.

Covers ALL market categories:
  sports      — SofaScore live feed + ESPN scoreboard entity match
  crypto      — CoinGecko 24h price change > 3% OR price near threshold
  politics    — election day heuristic + breaking news headlines
  economics   — FOMC/CPI/jobs release day + news check
  weather     — active storm/extreme weather alert
  general     — web search verifies event is current (fallback)

Fast: all checks run in parallel, hard 5s timeout.
Safe: any error returns False (never false-positive an alert).
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("trading.live_event_detector")

_TIMEOUT = httpx.Timeout(5.0)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _entities(title: str) -> list:
    """Extract meaningful named entities + keywords from a market title."""
    # Capitalised words (teams, people, countries)
    caps = re.findall(r"\b[A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,})*\b", title)
    # Numbers that look like price targets  e.g. 105000, 50k, $200k
    nums = re.findall(r"\$[\d,]+[kKmMbB]?|\b\d{4,}[kKmMbB]?\b", title)
    result = [e.lower() for e in caps[:6]] + nums[:2]
    return list(dict.fromkeys(result))


def _category(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["bitcoin","btc","eth","ethereum","solana","sol","xrp","crypto","doge","bnb"]):
        return "crypto"
    if any(k in t for k in ["nfl","nba","mlb","nhl","mls","soccer","football","basketball",
                              "baseball","hockey","tennis","ufc","mma","world cup","super bowl",
                              "champions league","game","match","score","playoff","championship"]):
        return "sports"
    if any(k in t for k in ["elect","midterm","primary","ballot","vote","senator","congress",
                              "president","governor","trump","biden","harris","republican","democrat"]):
        return "politics"
    if any(k in t for k in ["cpi","inflation","fomc","fed rate","federal reserve","unemployment",
                              "gdp","jobs report","treasury","yield","interest rate"]):
        return "economics"
    if any(k in t for k in ["hurricane","tornado","storm","flood","earthquake","wildfire",
                              "temperature","degrees","weather","blizzard"]):
        return "weather"
    return "general"


async def _get_json(url: str, params: dict = None) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS,
                                     follow_redirects=True) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.debug("_get_json %s failed: %s", url[:60], e)
        return None


# ── Category-specific live checks ────────────────────────────────────────────

async def _sports_live(title: str) -> bool:
    """Check SofaScore live events for entity match. Any sport."""
    from src.data.sports_fetcher import fetch_sofa_live, _SOFA_SPORTS

    ents = _entities(title)
    if not ents:
        return False

    sport_slugs = list(dict.fromkeys(_SOFA_SPORTS.values()))

    async def _check(slug: str) -> bool:
        try:
            from src.data.sports_fetcher import fetch_sofa_live
            events = await fetch_sofa_live(slug)
            for ev in events:
                combined = (
                    f"{ev.get('home','')} {ev.get('away','')} {ev.get('tournament','')}"
                ).lower()
                for ent in ents:
                    if len(ent) >= 4 and ent in combined:
                        logger.info("LIVE MATCH: '%s' found in '%s vs %s'",
                                    ent, ev.get("home"), ev.get("away"))
                        return True
        except Exception:
            pass
        return False

    results = await asyncio.gather(*[_check(s) for s in sport_slugs],
                                    return_exceptions=True)
    return any(r is True for r in results)


async def _crypto_live(title: str) -> bool:
    """
    Crypto is 'live' when the relevant coin is moving hard (>= 3% in 24h)
    OR price is within 5% of a target mentioned in the title.
    """
    t = title.lower()

    # Map title keywords to CoinGecko IDs
    coin_map = {
        "bitcoin": "bitcoin", "btc": "bitcoin",
        "ethereum": "ethereum", "eth": "ethereum",
        "solana": "solana", "sol": "solana",
        "xrp": "ripple", "ripple": "ripple",
        "dogecoin": "dogecoin", "doge": "dogecoin",
        "bnb": "binancecoin",
    }
    coin_ids = list({v for k, v in coin_map.items() if k in t})
    if not coin_ids:
        return False

    data = await _get_json(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": ",".join(coin_ids),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        },
    )
    if not data:
        return False

    for coin_id in coin_ids:
        info = data.get(coin_id, {})
        change_24h = abs(info.get("usd_24h_change") or 0)
        cur_price  = info.get("usd") or 0

        # Strong 24h move → market is "live" and worth a cheeky bid
        if change_24h >= 3.0:
            logger.info("CRYPTO LIVE: %s moving %.1f%% (24h)", coin_id, change_24h)
            return True

        # Price near a numeric target in the title
        targets = re.findall(r"(\d[\d,]*)[kK]?\b", title)
        for raw in targets:
            try:
                val = float(raw.replace(",", ""))
                # Handle "100k" style
                if re.search(rf"{raw}[kK]", title):
                    val *= 1000
                if val > 1000 and cur_price > 0:
                    pct_from_target = abs(cur_price - val) / val * 100
                    if pct_from_target <= 5:
                        logger.info("CRYPTO LIVE: %s at $%.0f, within 5%% of target $%.0f",
                                    coin_id, cur_price, val)
                        return True
            except Exception:
                pass

    return False


async def _politics_live(title: str) -> bool:
    """Election day or breaking political vote news."""
    now = datetime.now(timezone.utc)
    # US general election: first Tuesday of November, day 2–8
    if now.month == 11 and now.weekday() == 1 and 2 <= now.day <= 8:
        return True
    # US primary season: Tuesdays in March–June
    if now.month in (3, 4, 5, 6) and now.weekday() == 1:
        return True
    # Inauguration day: January 20
    if now.month == 1 and now.day == 20:
        return True
    # Check Google News for breaking election news today
    from urllib.parse import quote_plus
    ents = _entities(title)
    q = " ".join(ents[:3]) + " election results vote"
    try:
        from src.data.web_search import _google_news
        headlines = await _google_news(q)
        today_kws = ["today", "tonight", "now", "live", "results", "counting", "wins", "wins election"]
        for h in headlines:
            hl = h.lower()
            if any(k in hl for k in today_kws):
                logger.info("POLITICS LIVE: headline match → %s", h[:80])
                return True
    except Exception:
        pass
    return False


async def _economics_live(title: str) -> bool:
    """FOMC decision, CPI, jobs report happening today."""
    # Check for economic release keywords in today's news
    from urllib.parse import quote_plus
    t = title.lower()
    economic_kws = {
        "fomc": "fed decision interest rate",
        "federal reserve": "federal reserve rate decision",
        "cpi": "CPI inflation report",
        "inflation": "inflation report today",
        "jobs report": "jobs report employment today",
        "unemployment": "unemployment claims today",
        "gdp": "GDP report today",
    }
    query_terms = next(
        (v for k, v in economic_kws.items() if k in t), None
    )
    if not query_terms:
        return False
    try:
        from src.data.web_search import _google_news
        headlines = await _google_news(query_terms)
        live_kws = ["today", "now", "just", "released", "announced", "live", "decision"]
        for h in headlines:
            if any(k in h.lower() for k in live_kws):
                logger.info("ECONOMICS LIVE: headline → %s", h[:80])
                return True
    except Exception:
        pass
    return False


async def _weather_live(title: str) -> bool:
    """Active hurricane, tornado, or extreme weather event."""
    ents = _entities(title)
    if not ents:
        return False
    try:
        from src.data.web_search import _google_news
        weather_kws = ["hurricane", "tornado", "blizzard", "wildfire", "flood", "storm", "earthquake"]
        kw = next((k for k in weather_kws if k in title.lower()), "weather emergency")
        q  = f"{kw} " + " ".join(ents[:2])
        headlines = await _google_news(q)
        for h in headlines:
            if any(k in h.lower() for k in ["now", "active", "ongoing", "warning", "watch", "today", "live"]):
                logger.info("WEATHER LIVE: %s", h[:80])
                return True
    except Exception:
        pass
    return False


async def _news_confirms_live(title: str) -> bool:
    """
    Fallback: Google News search to check if the event in this title is
    generating real-time coverage right now.
    """
    try:
        from src.data.web_search import _google_news
        ents = _entities(title)
        if not ents:
            return False
        q = " ".join(ents[:4])
        headlines = await _google_news(q)
        now_kws = ["live", "now", "today", "tonight", "breaking", "just", "ongoing",
                   "in progress", "underway", "happening", "starts", "begins"]
        for h in headlines:
            if any(k in h.lower() for k in now_kws):
                logger.info("NEWS LIVE: '%s' → %s", q[:40], h[:80])
                return True
    except Exception:
        pass
    return False


# ── Main entry point ──────────────────────────────────────────────────────────

async def is_event_live_now(title: str) -> bool:
    """
    Return True if the real-world event behind this market is happening RIGHT NOW.

    Runs all relevant category checks in parallel, 5s hard timeout.
    Returns False on any error — never false-positive.
    """
    if not title or len(title) < 8:
        return False

    cat = _category(title)
    logger.debug("is_event_live_now: cat=%s title='%s'", cat, title[:60])

    # Always run: news confirmation (broad catch-all)
    # Plus category-specific check
    cat_check = {
        "sports":    _sports_live(title),
        "crypto":    _crypto_live(title),
        "politics":  _politics_live(title),
        "economics": _economics_live(title),
        "weather":   _weather_live(title),
        "general":   _news_confirms_live(title),
    }.get(cat, _news_confirms_live(title))

    try:
        results = await asyncio.wait_for(
            asyncio.gather(cat_check, _news_confirms_live(title),
                           return_exceptions=True),
            timeout=5.0,
        )
        return any(r is True for r in results)
    except asyncio.TimeoutError:
        logger.debug("is_event_live_now timed out for: %s", title[:60])
        return False
    except Exception as e:
        logger.debug("is_event_live_now error: %s", e)
        return False
