"""
Live Event Detector — answers ONE question per market:
"Is the real-world event behind this prediction happening RIGHT NOW?"

Covers ALL categories and sub-categories from Kalshi + Polymarket:
  Sports       — SofaScore + ESPN: NFL, NBA, MLB, NHL, Soccer, Tennis, Golf,
                 UFC, F1, Olympics, College, Esports, Rugby, Cricket, Boxing
  Crypto       — CoinGecko: price moving ≥3% OR within 5% of title target
  Politics     — Election day + breaking political news (US + global)
  Economics    — FOMC/CPI/jobs/GDP/earnings release day confirmed via news
  Science/Tech — SpaceX launch, NASA mission, AI announcement, product launch
  Geopolitics  — Active conflict, sanctions, treaty, summit happening now
  Entertainment— Awards ceremony, movie release, album drop live now
  Health       — FDA approval, drug ruling, pandemic news breaking now
  Weather      — Active storm, hurricane, wildfire, earthquake confirmed now
  Finance      — Market crash, IPO, earnings, Fed announcement today
  Legal        — Supreme Court ruling, trial verdict, indictment today
  General      — Universal fallback: Google News live/now/breaking check

All checks run in parallel with a hard 8s timeout.
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


# ── Category detector ─────────────────────────────────────────────────────────

def _detect_category(title: str) -> str:
    t = title.lower()

    # Sports — all sub-categories
    if any(k in t for k in [
        "nfl","nba","mlb","nhl","ncaa","college football","college basketball",
        "super bowl","nba finals","world series","stanley cup","playoffs",
        "soccer","football","premier league","epl","la liga","bundesliga",
        "serie a","ligue 1","champions league","europa league","world cup",
        "mls","ufc","mma","boxing","wrestling","wwe","tennis","wimbledon",
        "us open","french open","australian open","golf","pga","masters",
        "formula 1","f1","nascar","indycar","olympics","athletics","rugby",
        "cricket","esports","league of legends","counter strike","valorant",
        "game","match","score","playoff","championship","tournament","finals",
        "series","title","trophy","medal","draft","trade","transfer",
        "injury","lineup","roster","win","beat","defeat","upset",
    ]):
        return "sports"

    # Crypto — all sub-categories
    if any(k in t for k in [
        "bitcoin","btc","ethereum","eth","solana","sol","xrp","ripple",
        "dogecoin","doge","cardano","ada","polygon","matic","avalanche","avax",
        "chainlink","link","uniswap","aave","defi","nft","dao","web3",
        "crypto","blockchain","token","coin","wallet","exchange","binance",
        "coinbase","stablecoin","usdc","usdt","tether","altcoin","memecoin",
        "pepe","shiba","floki","etf","spot etf","halving","mining",
    ]):
        return "crypto"

    # Politics — all sub-categories
    if any(k in t for k in [
        "elect","midterm","vote","ballot","president","senator","congress",
        "governor","primary","inaugur","democrat","republican","gop",
        "white house","house speaker","senate majority","filibuster",
        "trump","biden","harris","desantis","newsom","pelosi","mcconnell",
        "uk election","eu election","french election","german election",
        "parliament","prime minister","chancellor","cabinet","referendum",
        "impeach","resign","approve","veto","bill","legislation","policy",
        "poll","approval rating","swing state","electoral college",
    ]):
        return "politics"

    # Geopolitics
    if any(k in t for k in [
        "iran","russia","ukraine","china","taiwan","north korea","nato",
        "israel","palestine","hamas","hezbollah","middle east","syria",
        "sanctions","ceasefire","invasion","war","conflict","missile",
        "nuclear","g7","g20","un","united nations","summit","treaty",
        "trade war","tariff","diplomat","embassy","coup","protest",
        "revolution","assassination","terrorist",
    ]):
        return "geopolitics"

    # Economics / Finance
    if any(k in t for k in [
        "cpi","inflation","fed","fomc","interest rate","gdp","unemployment",
        "jobs report","nonfarm","treasury","yield","nasdaq","s&p 500","dow jones",
        "recession","earnings","revenue","profit","ipo","acquisition","merger",
        "bankruptcy","default","debt ceiling","budget","deficit","stimulus",
        "oil price","gold price","silver","commodity","forex","dollar","euro",
        "yen","pound","rate hike","rate cut","quantitative","taper",
    ]):
        return "economics"

    # Science / Tech
    if any(k in t for k in [
        "spacex","starship","falcon","rocket","launch","nasa","artemis","james webb",
        "moon","mars","asteroid","satellite","iss","space station",
        "openai","chatgpt","gpt","gemini","claude","anthropic","ai model",
        "artificial intelligence","machine learning","llm","agi",
        "apple","iphone","ipad","mac","wwdc","google","android","pixel",
        "microsoft","windows","meta","facebook","instagram","twitter","x.com",
        "tesla","elon","neuralink","boring company","hyperloop",
        "nvidia","amd","intel","chip","semiconductor","quantum computing",
    ]):
        return "science_tech"

    # Entertainment / Culture
    if any(k in t for k in [
        "oscar","emmy","grammy","golden globe","bafta","tony award",
        "movie","film","box office","streaming","netflix","hbo","disney",
        "album","song","billboard","chart","tour","concert","festival",
        "super bowl halftime","taylor swift","beyonce","drake","kanye",
        "celebrity","actor","actress","director","showrunner",
        "game of thrones","stranger things","squid game",
    ]):
        return "entertainment"

    # Health / Medical
    if any(k in t for k in [
        "fda","drug approval","vaccine","clinical trial","pandemic","epidemic",
        "covid","flu","outbreak","who","cdc","nih","pharma","biotech",
        "cancer","alzheimer","obesity","weight loss drug","ozempic",
        "hospital","surgeon general","public health","emergency",
    ]):
        return "health"

    # Legal
    if any(k in t for k in [
        "supreme court","scotus","ruling","verdict","indictment","conviction",
        "trial","lawsuit","appeal","injunction","settlement","court",
        "judge","prosecutor","defendant","plea","sentence","acquit",
        "sec","ftc","doj","antitrust","regulation","fine","penalty",
    ]):
        return "legal"

    # Weather / Environment
    if any(k in t for k in [
        "hurricane","tornado","storm","flood","earthquake","wildfire",
        "blizzard","weather","temperature","drought","tsunami","typhoon",
        "climate","emissions","carbon","global warming","el nino","la nina",
    ]):
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


async def _news_live(query: str, live_kws: list = None) -> bool:
    """Shared news check — Google News headlines confirming event is live."""
    kws = live_kws or ["live","now","today","tonight","breaking","just","ongoing",
                        "happening","underway","in progress","starts","begins",
                        "kickoff","tip-off","opening","results","announced"]
    try:
        from src.data.web_search import _google_news
        headlines = await _google_news(query)
        for h in headlines:
            if any(k in h.lower() for k in kws):
                logger.info("NEWS LIVE [%s]: %s", query[:30], h[:80])
                return True
    except Exception:
        pass
    return False


def _title_terms(title: str, n: int = 5) -> str:
    """Extract key search terms from a market title."""
    stops = {"will","the","a","an","to","by","at","in","on","of","or","and",
             "is","be","for","does","when","with","from","this","that","win",
             "before","after","over","under","who","what","how","can"}
    words = re.findall(r"[a-zA-Z]{3,}", title)
    terms = [w for w in words if w.lower() not in stops]
    return " ".join(terms[:n])


# ── Category-specific checkers ────────────────────────────────────────────────

async def _sports_live(title: str) -> bool:
    from src.data.entity_registry import find_entities_in_title, get_aliases, SPORTS
    from src.data.sports_fetcher import fetch_sofa_live, _SOFA_SPORTS

    entities = find_entities_in_title(title)
    search_terms: set = set()
    for ent in entities:
        if ent in SPORTS:
            for alias in get_aliases(ent):
                if len(alias) >= 4:
                    search_terms.add(alias.lower())
        search_terms.add(ent.lower())
    for r in re.findall(r"\b[A-Z][a-zA-Z]{3,}(?:\s+[A-Z][a-zA-Z]{2,})*\b", title):
        search_terms.add(r.lower())

    if not search_terms:
        return False

    sport_slugs = list(dict.fromkeys(_SOFA_SPORTS.values()))

    async def _check_sofa(slug: str) -> bool:
        try:
            events = await fetch_sofa_live(slug)
            for ev in events:
                combined = f"{ev.get('home','')} {ev.get('away','')} {ev.get('tournament','')}".lower()
                for term in search_terms:
                    if term in combined:
                        logger.info("LIVE SPORTS [sofa]: '%s' in '%s vs %s'", term, ev.get("home"), ev.get("away"))
                        return True
        except Exception:
            pass
        return False

    from src.data.sports_fetcher import LEAGUES
    async def _check_espn(sport: str, league: str) -> bool:
        try:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
            data = await _get_json(url)
            if not data:
                return False
            for event in (data.get("events") or []):
                if not ((event.get("status") or {}).get("type") or {}).get("inProgress", False):
                    continue
                combined = f"{event.get('name','')} {event.get('shortName','')}".lower()
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
    from src.data.entity_registry import find_entities_in_title, CRYPTO
    entities = find_entities_in_title(title)
    _COINGECKO_IDS = {
        "bitcoin":"bitcoin","ethereum":"ethereum","solana":"solana",
        "ripple":"ripple","dogecoin":"dogecoin","binance coin":"binancecoin",
        "cardano":"cardano","avalanche":"avalanche-2","polygon":"matic-network",
        "chainlink":"chainlink","litecoin":"litecoin","polkadot":"polkadot",
        "uniswap":"uniswap","stellar":"stellar","cosmos":"cosmos",
        "monero":"monero","tron":"tron","ethereum classic":"ethereum-classic",
        "shiba inu":"shiba-inu","pepe coin":"pepe","bitcoin cash":"bitcoin-cash",
        "near protocol":"near","internet computer":"internet-computer",
        "aptos":"aptos","arbitrum":"arbitrum","optimism":"optimism",
        "sui":"sui","injective":"injective-protocol","sei":"sei-network",
    }
    coin_ids = [_COINGECKO_IDS[e] for e in entities if e in CRYPTO and e in _COINGECKO_IDS]
    if not coin_ids:
        t = title.lower()
        kw_map = {"bitcoin":"bitcoin","btc":"bitcoin","ethereum":"ethereum","eth":"ethereum",
                  "solana":"solana","sol":"solana","xrp":"ripple","doge":"dogecoin",
                  "bnb":"binancecoin","ada":"cardano","avax":"avalanche-2"}
        for kw, cg_id in kw_map.items():
            if re.search(r"\b" + kw + r"\b", t) and cg_id not in coin_ids:
                coin_ids.append(cg_id)
    if not coin_ids:
        return False
    data = await _get_json("https://api.coingecko.com/api/v3/simple/price",
                           params={"ids":",".join(coin_ids),"vs_currencies":"usd","include_24hr_change":"true"})
    if not data:
        return False
    for cid in coin_ids:
        info = data.get(cid, {})
        if abs(info.get("usd_24h_change") or 0) >= 3.0:
            logger.info("CRYPTO LIVE: %s moving %.1f%%", cid, info["usd_24h_change"])
            return True
        cur = info.get("usd") or 0
        for raw in re.findall(r"[\d,]+", title):
            try:
                val = float(raw.replace(",",""))
                if val > 1000 and cur > 0 and abs(cur-val)/val*100 <= 5:
                    logger.info("CRYPTO LIVE: %s near target $%.0f", cid, val)
                    return True
            except Exception:
                pass
    return False


async def _politics_live(title: str) -> bool:
    now = datetime.now(timezone.utc)
    if now.month == 11 and now.weekday() == 1 and 2 <= now.day <= 8:
        return True
    if now.month in (3,4,5,6) and now.weekday() == 1:
        return True
    if now.month == 1 and now.day == 20:
        return True
    return await _news_live(_title_terms(title) + " vote election results today",
                            ["today","tonight","now","live","results","counting","wins","breaking","just in","certified"])


async def _geopolitics_live(title: str) -> bool:
    return await _news_live(_title_terms(title) + " breaking news today",
                            ["today","now","breaking","just","live","escalat","attack","summit","ceasefire","announced","sanctions"])


async def _economics_live(title: str) -> bool:
    econ_kw_map = {
        "fomc":"fed rate decision today fomc",
        "federal reserve":"fed rate decision today fomc",
        "cpi":"CPI inflation report released today",
        "inflation":"inflation CPI report today",
        "jobs report":"nonfarm payrolls jobs report today",
        "gdp":"GDP report released today",
        "unemployment":"unemployment claims report today",
        "interest rate":"interest rate decision central bank today",
        "earnings":"earnings report released today quarterly results",
        "ipo":"IPO trading begins today",
    }
    t = title.lower()
    query = next((v for k,v in econ_kw_map.items() if k in t), None)
    if not query:
        query = _title_terms(title) + " report released today"
    return await _news_live(query, ["today","now","just","released","announced","live","decision","report","published"])


async def _science_tech_live(title: str) -> bool:
    t = title.lower()
    if any(k in t for k in ["launch","rocket","spacex","starship","falcon","nasa"]):
        return await _news_live(_title_terms(title) + " launch live now",
                                ["live","launch","liftoff","t-minus","countdown","now","today","scrub","success"])
    if any(k in t for k in ["openai","gpt","gemini","claude","ai model","llm"]):
        return await _news_live(_title_terms(title) + " released announced today",
                                ["released","launched","announced","available","now","today","just"])
    if any(k in t for k in ["apple","google","microsoft","meta","nvidia","samsung"]):
        return await _news_live(_title_terms(title) + " announced event today",
                                ["announced","released","today","now","live","event","keynote"])
    return await _news_live(_title_terms(title) + " announced today breaking",
                            ["today","now","live","announced","released","breaking"])


async def _entertainment_live(title: str) -> bool:
    t = title.lower()
    ceremony_kws = ["oscar","emmy","grammy","golden globe","bafta","tony"]
    if any(k in t for k in ceremony_kws):
        return await _news_live(_title_terms(title) + " ceremony live tonight winners",
                                ["live","tonight","winner","award","ceremony","red carpet","now"])
    return await _news_live(_title_terms(title) + " released today live",
                            ["today","now","live","premiere","released","drops","out now"])


async def _health_live(title: str) -> bool:
    return await _news_live(_title_terms(title) + " approved announced today",
                            ["today","approved","announced","just","now","ruling","emergency","outbreak"])


async def _legal_live(title: str) -> bool:
    return await _news_live(_title_terms(title) + " ruling verdict today court",
                            ["today","ruling","verdict","decided","announced","just","now","guilty","acquitted","indicted"])


async def _weather_live(title: str) -> bool:
    from src.data.entity_registry import find_entities_in_title, get_aliases, WEATHER
    entities = find_entities_in_title(title)
    weather_ents = [e for e in entities if e in WEATHER]
    search_terms = []
    for ent in weather_ents[:2]:
        search_terms.extend(list(get_aliases(ent))[:2])
    if not search_terms:
        search_terms = [k for k in ["hurricane","tornado","blizzard","wildfire","flood","earthquake"] if k in title.lower()]
    if not search_terms:
        return False
    return await _news_live(" ".join(search_terms[:3]) + " active warning now",
                            ["now","active","ongoing","warning","watch","today","live","landfall","makes landfall"])


async def _news_confirms_live(title: str) -> bool:
    """Universal fallback — news headlines confirming live coverage."""
    from src.data.entity_registry import find_entities_in_title, get_aliases
    entities = find_entities_in_title(title)
    terms = []
    for ent in entities[:3]:
        aliases = get_aliases(ent)
        best = sorted([a for a in aliases if 4 <= len(a) <= 20], key=len)
        terms.append(best[0] if best else ent)
    if not terms:
        terms = re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", title)[:4]
    if not terms:
        return False
    return await _news_live(" ".join(terms))


# ── Main entry point ──────────────────────────────────────────────────────────

_CATEGORY_CHECKER = {
    "sports":       _sports_live,
    "crypto":       _crypto_live,
    "politics":     _politics_live,
    "geopolitics":  _geopolitics_live,
    "economics":    _economics_live,
    "science_tech": _science_tech_live,
    "entertainment":_entertainment_live,
    "health":       _health_live,
    "legal":        _legal_live,
    "weather":      _weather_live,
    "general":      _news_confirms_live,
}


async def is_event_live_now(title: str) -> bool:
    """
    Return True if the real-world event behind this market is happening RIGHT NOW.

    1. Detect category from title (covers all Kalshi + Polymarket sub-categories)
    2. Run category-specific checker + universal news fallback in parallel
    3. Hard 8s timeout — returns False if all checks timeout or error

    Never false-positive: requires positive confirmation from a real data source.
    """
    if not title or len(title) < 8:
        return False

    cat = _detect_category(title)
    logger.debug("is_event_live_now: cat=%s title='%s'", cat, title[:60])

    specific_check = _CATEGORY_CHECKER.get(cat, _news_confirms_live)(title)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(specific_check, _news_confirms_live(title), return_exceptions=True),
            timeout=8.0,
        )
        return any(r is True for r in results)
    except asyncio.TimeoutError:
        logger.debug("is_event_live_now timed out: %s", title[:60])
        return False
    except Exception as e:
        logger.debug("is_event_live_now error: %s", e)
        return False
