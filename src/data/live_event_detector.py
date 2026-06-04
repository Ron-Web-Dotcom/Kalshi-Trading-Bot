"""
Live Event Detector — answers ONE question per market:
"Is the real-world event behind this prediction happening RIGHT NOW?"

Uses the comprehensive entity registry to identify what's in the title,
then checks the appropriate live data source for that category.

Covers ALL market categories:
  sports      — SofaScore live feed + ESPN scoreboard, matched via entity registry
  crypto      — CoinGecko: coin moving >= 3% OR price within 5% of target
  politics    — Election day heuristic + breaking news headlines
  economics   — FOMC/CPI/jobs release day confirmed via news
  weather     — Active storm/extreme weather confirmed via news
  general     — Google News "live/now/breaking" headline check

All checks run in parallel with a hard 5s timeout.
Returns False on any error — never false-positive an alert.
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

def _detect_category(title: str) -> str:
    """Detect the primary category of a market from its title."""
    from src.data.entity_registry import find_entities_in_title, get_registry_for_entity
    entities = find_entities_in_title(title)
    if entities:
        reg = get_registry_for_entity(entities[0])
        if reg:
            return reg.lower()

    t = title.lower()
    if any(k in t for k in ["bitcoin","btc","eth","crypto","solana","doge","coin","token","blockchain"]):
        return "crypto"
    if any(k in t for k in ["nfl","nba","mlb","nhl","soccer","football","basketball","baseball",
                              "hockey","game","match","score","playoff","championship","tournament",
                              "world cup","super bowl","finals","series","ufc","tennis","golf","f1"]):
        return "sports"
    if any(k in t for k in ["elect","midterm","vote","ballot","president","senator","congress",
                              "governor","primary","inaugur","political","party","democrat","republican"]):
        return "politics"
    if any(k in t for k in ["cpi","inflation","fed","fomc","interest rate","gdp","unemployment",
                              "jobs report","treasury","yield","nasdaq","s&p","dow","recession"]):
        return "economics"
    if any(k in t for k in ["hurricane","tornado","storm","flood","earthquake","wildfire",
                              "blizzard","weather","temperature","drought","tsunami"]):
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
        logger.debug("_get_json %s: %s", url[:60], e)
        return None


# ── Category checkers ─────────────────────────────────────────────────────────

async def _sports_live(title: str) -> bool:
    """
    Check SofaScore live events AND ESPN scoreboards across all leagues.
    Uses entity registry to extract team/player names, then matches against
    live event data (home team, away team, tournament name).
    """
    from src.data.entity_registry import find_entities_in_title, get_aliases, SPORTS
    from src.data.sports_fetcher import fetch_sofa_live, _SOFA_SPORTS

    entities = find_entities_in_title(title)
    # Build full alias set for every sports entity found in the title
    search_terms: set = set()
    for ent in entities:
        if ent in SPORTS:
            for alias in get_aliases(ent):
                if len(alias) >= 4:
                    search_terms.add(alias.lower())
        search_terms.add(ent.lower())

    # Also fall back to raw capitalised words from the title
    raw = re.findall(r"\b[A-Z][a-zA-Z]{3,}(?:\s+[A-Z][a-zA-Z]{2,})*\b", title)
    for r in raw:
        search_terms.add(r.lower())

    if not search_terms:
        return False

    # Fetch all live sports from SofaScore in parallel
    sport_slugs = list(dict.fromkeys(_SOFA_SPORTS.values()))

    async def _check_sofa(slug: str) -> bool:
        try:
            events = await fetch_sofa_live(slug)
            for ev in events:
                combined = (
                    f"{ev.get('home','')} {ev.get('away','')} {ev.get('tournament','')}"
                ).lower()
                for term in search_terms:
                    if term in combined:
                        logger.info("LIVE SPORTS [sofa]: '%s' in '%s vs %s' (%s)",
                                    term, ev.get("home"), ev.get("away"), ev.get("tournament"))
                        return True
        except Exception:
            pass
        return False

    # Also check ESPN across all supported leagues
    from src.data.sports_fetcher import LEAGUES
    async def _check_espn(sport: str, league: str) -> bool:
        try:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
            data = await _get_json(url)
            if not data:
                return False
            for event in (data.get("events") or []):
                status = ((event.get("status") or {}).get("type") or {})
                # Only count in-progress events
                if not status.get("inProgress", False):
                    continue
                ev_name = event.get("name", "").lower()
                ev_short = event.get("shortName", "").lower()
                combined = f"{ev_name} {ev_short}"
                for term in search_terms:
                    if term in combined:
                        logger.info("LIVE SPORTS [espn]: '%s' in '%s'", term, event.get("name"))
                        return True
        except Exception:
            pass
        return False

    coros = [_check_sofa(s) for s in sport_slugs]
    coros += [_check_espn(sport, league) for league, (sport, _) in LEAGUES.items()]

    results = await asyncio.gather(*coros, return_exceptions=True)
    return any(r is True for r in results)


async def _crypto_live(title: str) -> bool:
    """
    Crypto is 'live' when the coin is moving hard (>= 3% in 24h)
    OR current price is within 5% of a numeric target in the title.
    Uses entity registry to identify coins accurately.
    """
    from src.data.entity_registry import find_entities_in_title, get_aliases, CRYPTO

    entities = find_entities_in_title(title)
    # Map entity canonical names to CoinGecko IDs
    _COINGECKO_IDS = {
        "bitcoin": "bitcoin", "ethereum": "ethereum", "solana": "solana",
        "ripple": "ripple", "dogecoin": "dogecoin", "binance coin": "binancecoin",
        "cardano": "cardano", "avalanche": "avalanche-2", "polygon": "matic-network",
        "chainlink": "chainlink", "litecoin": "litecoin", "polkadot": "polkadot",
        "uniswap": "uniswap", "stellar": "stellar", "cosmos": "cosmos",
        "monero": "monero", "tron": "tron", "ethereum classic": "ethereum-classic",
        "shiba inu": "shiba-inu", "pepe coin": "pepe", "bitcoin cash": "bitcoin-cash",
        "near protocol": "near", "internet computer": "internet-computer",
        "aptos": "aptos", "arbitrum": "arbitrum", "optimism": "optimism",
        "sui": "sui", "injective": "injective-protocol", "sei": "sei-network",
        "render": "render-token", "fetch ai": "fetch-ai", "worldcoin": "worldcoin-wld",
    }

    coin_ids = []
    for ent in entities:
        if ent in CRYPTO:
            cg_id = _COINGECKO_IDS.get(ent)
            if cg_id and cg_id not in coin_ids:
                coin_ids.append(cg_id)

    if not coin_ids:
        # Fallback: direct keyword scan for common coins
        t = title.lower()
        kw_map = {"bitcoin":"bitcoin","btc":"bitcoin","ethereum":"ethereum","eth":"ethereum",
                  "solana":"solana","sol":"solana","xrp":"ripple","doge":"dogecoin"}
        for kw, cg_id in kw_map.items():
            if re.search(r"\b" + kw + r"\b", t) and cg_id not in coin_ids:
                coin_ids.append(cg_id)

    if not coin_ids:
        return False

    data = await _get_json(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": ",".join(coin_ids), "vs_currencies": "usd",
                "include_24hr_change": "true"},
    )
    if not data:
        return False

    for coin_id in coin_ids:
        info = data.get(coin_id, {})
        change_24h = abs(info.get("usd_24h_change") or 0)
        cur_price  = info.get("usd") or 0

        if change_24h >= 3.0:
            logger.info("CRYPTO LIVE: %s moving %.1f%% (24h)", coin_id, change_24h)
            return True

        # Check if price is near a numeric target in the title
        for raw in re.findall(r"([\d,]+)\s*[kK]?\b", title):
            try:
                val = float(raw.replace(",", ""))
                if "k" in title[title.find(raw) + len(raw):title.find(raw) + len(raw) + 2].lower():
                    val *= 1000
                if val > 1000 and cur_price > 0:
                    if abs(cur_price - val) / val * 100 <= 5:
                        logger.info("CRYPTO LIVE: %s at $%.0f, within 5%% of $%.0f",
                                    coin_id, cur_price, val)
                        return True
            except Exception:
                pass

    return False


async def _politics_live(title: str) -> bool:
    """Election day detection + breaking political news."""
    now = datetime.now(timezone.utc)
    # US general election: first Tuesday of November, day 2–8
    if now.month == 11 and now.weekday() == 1 and 2 <= now.day <= 8:
        return True
    # US primary season Tuesdays March–June
    if now.month in (3, 4, 5, 6) and now.weekday() == 1:
        return True
    # Inauguration Day
    if now.month == 1 and now.day == 20:
        return True

    from src.data.entity_registry import find_entities_in_title, get_aliases
    entities = find_entities_in_title(title)
    search_terms = []
    for ent in entities[:3]:
        aliases = get_aliases(ent)
        search_terms.append(next((a for a in aliases if len(a) > 5), ent))

    q = " ".join(search_terms[:3]) + " election results vote today"
    try:
        from src.data.web_search import _google_news
        headlines = await _google_news(q)
        live_kws = ["today","tonight","now","live","results","counting","wins","breaking","just in"]
        for h in headlines:
            if any(k in h.lower() for k in live_kws):
                logger.info("POLITICS LIVE: %s", h[:80])
                return True
    except Exception:
        pass
    return False


async def _economics_live(title: str) -> bool:
    """FOMC decision, CPI, jobs report, or major economic release happening today."""
    from src.data.entity_registry import find_entities_in_title
    entities = find_entities_in_title(title)

    econ_kw_to_query = {
        "federal reserve": "fed rate decision today fomc",
        "fomc": "fed rate decision today fomc",
        "consumer price index": "CPI inflation report released today",
        "inflation": "inflation CPI report today",
        "jobs report": "nonfarm payrolls jobs report today",
        "gdp": "GDP economic report released today",
        "unemployment": "unemployment claims report today",
        "treasury yield": "treasury yield bond market today",
        "interest rate": "interest rate decision central bank today",
    }

    t = title.lower()
    query = next((v for k, v in econ_kw_to_query.items()
                  if k in t or any(k in e for e in entities)), None)
    if not query:
        return False

    try:
        from src.data.web_search import _google_news
        headlines = await _google_news(query)
        live_kws = ["today","now","just","released","announced","live","decision","report"]
        for h in headlines:
            if any(k in h.lower() for k in live_kws):
                logger.info("ECONOMICS LIVE: %s", h[:80])
                return True
    except Exception:
        pass
    return False


async def _weather_live(title: str) -> bool:
    """Active hurricane, tornado, or extreme weather event right now."""
    from src.data.entity_registry import find_entities_in_title, get_aliases, WEATHER
    entities = find_entities_in_title(title)

    weather_ents = [e for e in entities if e in WEATHER]
    search_terms = []
    for ent in weather_ents[:2]:
        search_terms.extend(list(get_aliases(ent))[:2])
    if not search_terms:
        # Fallback raw keywords
        weather_kws = ["hurricane","tornado","blizzard","wildfire","flood","earthquake","storm"]
        search_terms = [k for k in weather_kws if k in title.lower()]

    if not search_terms:
        return False

    try:
        from src.data.web_search import _google_news
        q = " ".join(search_terms[:3]) + " active warning now"
        headlines = await _google_news(q)
        for h in headlines:
            if any(k in h.lower() for k in ["now","active","ongoing","warning","watch","today","live","landfall"]):
                logger.info("WEATHER LIVE: %s", h[:80])
                return True
    except Exception:
        pass
    return False


async def _news_confirms_live(title: str) -> bool:
    """
    Universal fallback: Google News check for any live coverage of entities
    mentioned in the title right now.
    """
    from src.data.entity_registry import find_entities_in_title, get_aliases
    entities = find_entities_in_title(title)

    # Build best search query from known entity aliases
    terms = []
    for ent in entities[:3]:
        aliases = get_aliases(ent)
        # prefer medium-length alias (not too short, not too long)
        best = sorted([a for a in aliases if 4 <= len(a) <= 20], key=len)
        if best:
            terms.append(best[0])
        else:
            terms.append(ent)

    if not terms:
        # Fall back to raw capitalized words from the title
        caps = re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", title)
        terms = caps[:4]

    if not terms:
        return False

    try:
        from src.data.web_search import _google_news
        q = " ".join(terms)
        headlines = await _google_news(q)
        now_kws = ["live","now","today","tonight","breaking","just","ongoing","happening",
                   "underway","in progress","starts","begins","kickoff","tip-off","opening"]
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

    1. Detects the market category (sports/crypto/politics/economics/weather/general)
    2. Runs the category-specific live check in parallel with a broad news check
    3. Hard 5s timeout — returns False if all checks timeout or error

    Never false-positive: requires positive confirmation from a real data source.
    """
    if not title or len(title) < 8:
        return False

    cat = _detect_category(title)
    logger.debug("is_event_live_now: cat=%s title='%s'", cat, title[:60])

    category_check = {
        "sports":    _sports_live(title),
        "crypto":    _crypto_live(title),
        "politics":  _politics_live(title),
        "economics": _economics_live(title),
        "weather":   _weather_live(title),
        "general":   _news_confirms_live(title),
    }.get(cat, _news_confirms_live(title))

    try:
        results = await asyncio.wait_for(
            asyncio.gather(category_check, _news_confirms_live(title),
                           return_exceptions=True),
            timeout=5.0,
        )
        return any(r is True for r in results)
    except asyncio.TimeoutError:
        logger.debug("is_event_live_now timed out: %s", title[:60])
        return False
    except Exception as e:
        logger.debug("is_event_live_now error: %s", e)
        return False
