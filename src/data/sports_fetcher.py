"""
Sports data via ESPN's public JSON API + SofaScore API — no API key required.

Fetches live/recent scores, standings, and team records to inform AI
decisions on Kalshi sports markets:
  "Will the Chiefs win Super Bowl LX?"
  "Will Manchester City win the Premier League?"
  "Will Real Madrid reach the Champions League final?"
  "Will the Lakers make the NBA playoffs?"

Endpoints used (all public, no auth):
  ESPN:      https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard
  SofaScore: https://api.sofascore.com/api/v1/sport/{sport}/events/live
             https://api.sofascore.com/api/v1/search/all?q={query}
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("trading.sports_fetcher")

_TIMEOUT      = httpx.Timeout(10.0)
_BASE         = "https://site.api.espn.com/apis/site/v2/sports"
_SOFA_BASE    = "https://api.sofascore.com/api/v1"
_SOFA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Referer":    "https://www.sofascore.com/",
}

# SofaScore sport slugs
_SOFA_SPORTS = {
    "nfl": "american-football", "nba": "basketball", "mlb": "baseball",
    "nhl": "ice-hockey",        "nba": "basketball", "ufc": "mma",
    "epl": "football",          "laliga": "football", "bundesliga": "football",
    "seriea": "football",       "ligue1": "football", "ucl": "football",
    "uel": "football",          "mls": "football",    "worldcup": "football",
}

# Supported leagues with ESPN sport/league path
LEAGUES: Dict[str, Tuple[str, str]] = {
    # American sports
    "nfl":       ("football",   "nfl"),
    "nba":       ("basketball", "nba"),
    "mlb":       ("baseball",   "mlb"),
    "nhl":       ("hockey",     "nhl"),
    "ncaaf":     ("football",   "college-football"),
    "ncaab":     ("basketball", "mens-college-basketball"),
    "ufc":       ("mma",        "ufc"),
    # Soccer — US
    "mls":       ("soccer",     "usa.1"),
    "nwsl":      ("soccer",     "usa.nwsl"),
    # Soccer — Europe (top leagues + continental)
    "epl":       ("soccer",     "eng.1"),        # Premier League
    "championship": ("soccer",  "eng.2"),        # English Championship
    "laliga":    ("soccer",     "esp.1"),         # La Liga
    "bundesliga": ("soccer",    "ger.1"),         # Bundesliga
    "seriea":    ("soccer",     "ita.1"),         # Serie A
    "ligue1":    ("soccer",     "fra.1"),         # Ligue 1
    "eredivisie": ("soccer",    "ned.1"),         # Eredivisie
    "primeiraliga": ("soccer",  "por.1"),         # Primeira Liga
    "superlig":  ("soccer",     "tur.1"),         # Turkish Süper Lig
    # Soccer — continental/world
    "ucl":       ("soccer",     "uefa.champions"), # UEFA Champions League
    "uel":       ("soccer",     "uefa.europa"),    # UEFA Europa League
    "uecl":      ("soccer",     "uefa.europa.conf"), # UEFA Conference League
    "worldcup":  ("soccer",     "fifa.world"),     # FIFA World Cup
    "euros":     ("soccer",     "uefa.euro"),      # UEFA Euros
    "copalibertadores": ("soccer", "conmebol.libertadores"),
    "concacaf":  ("soccer",     "concacaf.champions"),
}

# Common team aliases → ESPN team abbreviation or search term
TEAM_ALIASES: Dict[str, str] = {
    # NFL
    "chiefs":      "KC",   "eagles":     "PHI", "cowboys":    "DAL",
    "patriots":    "NE",   "packers":    "GB",  "bears":      "CHI",
    "steelers":    "PIT",  "ravens":     "BAL", "broncos":    "DEN",
    "raiders":     "LV",   "chargers":   "LAC", "niners":     "SF",
    "49ers":       "SF",   "rams":       "LAR", "seahawks":   "SEA",
    "cardinals":   "ARI",  "giants":     "NYG", "jets":       "NYJ",
    "bills":       "BUF",  "dolphins":   "MIA", "titans":     "TEN",
    "colts":       "IND",  "jaguars":    "JAX", "texans":     "HOU",
    "bengals":     "CIN",  "browns":     "CLE", "vikings":    "MIN",
    "lions":       "DET",  "falcons":    "ATL", "saints":     "NO",
    "buccaneers":  "TB",   "panthers":   "CAR", "commanders": "WSH",
    # NBA
    "lakers":      "LAL",  "celtics":    "BOS", "warriors":   "GSW",
    "heat":        "MIA",  "bulls":      "CHI", "knicks":     "NYK",
    "nets":        "BKN",  "76ers":      "PHI", "suns":       "PHX",
    "nuggets":     "DEN",  "bucks":      "MIL", "clippers":   "LAC",
    "spurs":       "SAS",  "mavericks":  "DAL", "mavs":       "DAL",
    "rockets":     "HOU",  "thunder":    "OKC", "jazz":       "UTA",
    "blazers":     "POR",  "kings":      "SAC", "pelicans":   "NOP",
    "grizzlies":   "MEM",  "hawks":      "ATL", "hornets":    "CHA",
    "magic":       "ORL",  "pistons":    "DET", "cavaliers":  "CLE",
    "cavs":        "CLE",  "raptors":    "TOR", "wolves":     "MIN",
    "timberwolves":"MIN",  "pacers":     "IND",
    # MLB
    "yankees":     "NYY",  "red sox":    "BOS", "dodgers":    "LAD",
    "cubs":        "CHC",  "mets":       "NYM", "giants":     "SF",
    "braves":      "ATL",  "astros":     "HOU", "cardinals":  "STL",
    "phillies":    "PHI",  "blue jays":  "TOR", "padres":     "SD",
    "brewers":     "MIL",  "mariners":   "SEA", "nationals":  "WSH",
    "tigers":      "DET",  "white sox":  "CWS", "twins":      "MIN",
    "athletics":   "OAK",  "angels":     "LAA", "rangers":    "TEX",
    "royals":      "KC",   "orioles":    "BAL", "pirates":    "PIT",
    "reds":        "CIN",  "rockies":    "COL", "diamondbacks":"ARI",
    "marlins":     "MIA",  "rays":       "TB",
    # NHL
    "bruins":      "BOS",  "canadiens":  "MTL", "maple leafs":"TOR",
    "rangers":     "NYR",  "islanders":  "NYI", "flyers":     "PHI",
    "penguins":    "PIT",  "capitals":   "WSH", "blackhawks": "CHI",
    "red wings":   "DET",  "blues":      "STL", "wild":       "MIN",
    "predators":   "NSH",  "lightning":  "TB",  "panthers":   "FLA",
    "hurricanes":  "CAR",  "avalanche":  "COL", "golden knights": "VGK",
    "oilers":      "EDM",  "flames":     "CGY", "canucks":    "VAN",
    "sharks":      "SJS",  "ducks":      "ANA", "kings":      "LAK",
    "coyotes":     "ARI",  "jets":       "WPG", "senators":   "OTT",

    # Soccer — Premier League (EPL)
    "arsenal":         "ARS",  "chelsea":       "CHE",  "liverpool":    "LIV",
    "manchester city": "MCI",  "man city":      "MCI",  "man utd":      "MUN",
    "manchester united":"MUN", "tottenham":     "TOT",  "spurs":        "TOT",
    "newcastle":       "NEW",  "aston villa":   "AVL",  "west ham":     "WHU",
    "brighton":        "BHA",  "everton":       "EVE",  "fulham":       "FUL",
    "brentford":       "BRE",  "crystal palace":"CRY",  "wolverhampton":"WOL",
    "wolves":          "WOL",  "nottingham":    "NFO",  "leicester":    "LEI",
    "southampton":     "SOU",  "ipswich":       "IPS",  "bournemouth":  "BOU",

    # Soccer — La Liga
    "real madrid":     "RM",   "barcelona":     "BAR",  "atletico":     "ATM",
    "atletico madrid": "ATM",  "sevilla":       "SEV",  "real sociedad":"RSO",
    "villarreal":      "VIL",  "athletic bilbao":"ATH", "valencia":     "VAL",
    "real betis":      "BET",  "osasuna":       "OSA",  "getafe":       "GET",
    "girona":          "GIR",  "las palmas":    "LPA",  "mallorca":     "MLL",

    # Soccer — Bundesliga
    "bayern":          "BAY",  "bayern munich": "BAY",  "dortmund":     "BVB",
    "borussia dortmund":"BVB", "leverkusen":    "B04",  "rb leipzig":   "RBL",
    "frankfurt":       "SGE",  "wolfsburg":     "WOB",  "freiburg":     "SCF",
    "stuttgart":       "VFB",  "hoffenheim":    "TSG",  "gladbach":     "BMG",
    "borussia monchengladbach":"BMG",

    # Soccer — Serie A
    "inter milan":     "INT",  "inter":         "INT",  "juventus":     "JUV",
    "ac milan":        "MIL",  "milan":         "MIL",  "napoli":       "NAP",
    "roma":            "ROM",  "lazio":         "LAZ",  "atalanta":     "ATA",
    "fiorentina":      "FIO",  "bologna":       "BOL",  "torino":       "TOR",

    # Soccer — Ligue 1
    "psg":             "PSG",  "paris saint-germain":"PSG", "marseille": "OM",
    "monaco":          "ASM",  "lyon":          "OL",   "lille":        "LIL",
    "nice":            "OGC",  "rennes":        "REN",  "lens":         "RCL",

    # Soccer — MLS
    "inter miami":     "MIA",  "lafc":          "LAFC", "la galaxy":    "LA",
    "seattle sounders":"SEA",  "portland timbers":"POR", "new york city":"NYC",
    "nycfc":           "NYC",  "new england revolution":"NE",
    "atlanta united":  "ATL",  "columbus crew": "CLB",  "toronto fc":   "TOR",
    "cf montreal":     "MTL",  "orlando city":  "ORL",  "nashville sc": "NSH",
    "fc cincinnati":   "CIN",  "chicago fire":  "CHI",  "austin fc":    "ATX",
    "real salt lake":  "RSL",  "colorado rapids":"COL", "san jose earthquakes":"SJ",
    "houston dynamo":  "HOU",  "vancouver whitecaps":"VAN", "minnesota united":"MIN",
}

# League detection patterns — checked in order; first match wins
_LEAGUE_PATTERNS: Dict[str, List[str]] = {
    # Continental / world soccer first (most specific)
    "ucl":          ["champions league", "ucl", "champion league"],
    "uel":          ["europa league", "uel"],
    "uecl":         ["conference league", "uecl"],
    "worldcup":     ["world cup", "fifa world", "world cup qualifier"],
    "euros":        ["euro 2024", "euros", "european championship", "euro qualifier"],
    "copalibertadores": ["copa libertadores", "libertadores"],
    "concacaf":     ["concacaf champions"],
    # Domestic soccer leagues
    "epl":          ["premier league", "epl", "english premier", "fa cup"],
    "laliga":       ["la liga", "laliga", "spanish league"],
    "bundesliga":   ["bundesliga", "german league"],
    "seriea":       ["serie a", "italian league", "coppa italia"],
    "ligue1":       ["ligue 1", "ligue1", "french league"],
    "mls":          ["mls", "major league soccer"],
    # American sports
    "nfl":          ["nfl", "super bowl", "football", "touchdown", "quarterback"],
    "nba":          ["nba", "basketball", "nba finals"],
    "mlb":          ["mlb", "baseball", "world series", "innings", "batting"],
    "nhl":          ["nhl", "hockey", "stanley cup", "goalie", "puck"],
    "ncaaf":        ["ncaa football", "college football", "cfp", "bowl game"],
    "ncaab":        ["ncaa basketball", "march madness", "final four"],
    "ufc":          ["ufc", "mma", "octagon"],
}


_EPL_TEAMS = {
    "arsenal", "chelsea", "liverpool", "manchester city", "man city", "man utd",
    "manchester united", "tottenham", "spurs", "newcastle", "aston villa", "west ham",
    "brighton", "everton", "fulham", "brentford", "crystal palace", "wolverhampton",
    "wolves", "nottingham", "leicester", "southampton", "ipswich", "bournemouth",
}
_LALIGA_TEAMS = {
    "real madrid", "barcelona", "atletico", "atletico madrid", "sevilla",
    "real sociedad", "villarreal", "athletic bilbao", "valencia", "real betis",
    "osasuna", "getafe", "girona",
}
_BUNDESLIGA_TEAMS = {
    "bayern", "bayern munich", "dortmund", "borussia dortmund", "leverkusen",
    "rb leipzig", "frankfurt", "wolfsburg", "freiburg", "stuttgart", "hoffenheim",
    "gladbach", "borussia monchengladbach",
}
_SERIEA_TEAMS = {
    "inter milan", "inter", "juventus", "ac milan", "milan", "napoli",
    "roma", "lazio", "atalanta", "fiorentina", "bologna",
}
_LIGUE1_TEAMS = {"psg", "paris saint-germain", "marseille", "monaco", "lyon", "lille", "nice"}
_NBA_TEAMS = {
    "lakers", "celtics", "warriors", "heat", "bulls", "knicks", "nets", "76ers",
    "suns", "nuggets", "bucks", "clippers", "spurs", "mavericks", "mavs", "rockets",
    "thunder", "jazz", "blazers", "kings", "pelicans", "grizzlies", "hawks", "hornets",
    "magic", "pistons", "cavaliers", "cavs", "raptors", "wolves", "timberwolves", "pacers",
}
_MLB_TEAMS = {
    "yankees", "red sox", "dodgers", "cubs", "mets", "braves", "astros", "phillies",
    "blue jays", "padres", "brewers", "mariners", "nationals", "tigers", "white sox",
    "twins", "athletics", "angels", "rangers", "royals", "orioles", "pirates",
    "reds", "rockies", "diamondbacks", "marlins", "rays",
}
_NHL_TEAMS = {
    "bruins", "canadiens", "maple leafs", "islanders", "flyers", "penguins", "capitals",
    "blackhawks", "red wings", "blues", "wild", "predators", "lightning", "hurricanes",
    "avalanche", "golden knights", "oilers", "flames", "canucks", "sharks", "ducks",
    "coyotes", "senators",
}
_MLS_TEAMS = {
    "inter miami", "lafc", "la galaxy", "seattle sounders", "portland timbers",
    "new york city", "nycfc", "new england revolution", "atlanta united",
    "columbus crew", "toronto fc", "cf montreal", "orlando city", "nashville sc",
    "fc cincinnati", "chicago fire", "austin fc", "real salt lake", "colorado rapids",
    "san jose earthquakes", "houston dynamo", "vancouver whitecaps", "minnesota united",
}


def detect_league(title: str) -> Optional[str]:
    """Detect which sports league a market title refers to."""
    t = title.lower()
    # Pattern-based (most reliable — explicit league names)
    for league, patterns in _LEAGUE_PATTERNS.items():
        if any(p in t for p in patterns):
            return league
    # Team-based fallback
    for alias in TEAM_ALIASES:
        if not re.search(r'\b' + re.escape(alias) + r'\b', t):
            continue
        if alias in _EPL_TEAMS:      return "epl"
        if alias in _LALIGA_TEAMS:   return "laliga"
        if alias in _BUNDESLIGA_TEAMS: return "bundesliga"
        if alias in _SERIEA_TEAMS:   return "seriea"
        if alias in _LIGUE1_TEAMS:   return "ligue1"
        if alias in _MLS_TEAMS:      return "mls"
        if alias in _NBA_TEAMS:      return "nba"
        if alias in _MLB_TEAMS:      return "mlb"
        if alias in _NHL_TEAMS:      return "nhl"
        # Default NFL for remaining American football teams
        return "nfl"
    return None


def extract_teams(title: str) -> List[str]:
    """Extract team abbreviations mentioned in a market title."""
    t = title.lower()
    found = []
    for alias, abbrev in TEAM_ALIASES.items():
        if re.search(r'\b' + re.escape(alias) + r'\b', t):
            if abbrev not in found:
                found.append(abbrev)
    return found


async def fetch_scoreboard(league: str) -> List[Dict]:
    """Fetch recent/live scores for a league."""
    config = LEAGUES.get(league)
    if not config:
        return []
    sport, league_path = config
    url = f"{_BASE}/{sport}/{league_path}/scoreboard"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()

        games = []
        for event in data.get("events", [])[:10]:
            comp = event.get("competitions", [{}])[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue
            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
            status = event.get("status", {}).get("type", {})
            games.append({
                "home_team":  home.get("team", {}).get("abbreviation", ""),
                "home_score": home.get("score", ""),
                "away_team":  away.get("team", {}).get("abbreviation", ""),
                "away_score": away.get("score", ""),
                "status":     status.get("description", ""),
                "completed":  status.get("completed", False),
            })
        return games
    except Exception as e:
        logger.debug("Scoreboard fetch failed for %s: %s", league, e)
        return []


async def fetch_standings(league: str) -> List[Dict]:
    """Fetch current standings for a league."""
    config = LEAGUES.get(league)
    if not config:
        return []
    sport, league_path = config
    url = f"{_BASE}/{sport}/{league_path}/standings"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()

        entries = []
        for group in data.get("children", [data]):
            for standing in group.get("standings", {}).get("entries", []):
                team  = standing.get("team", {}).get("abbreviation", "")
                stats = {s["name"]: s.get("displayValue", "") for s in standing.get("stats", [])}
                if team:
                    entries.append({"team": team, "stats": stats})
        return entries[:16]
    except Exception as e:
        logger.debug("Standings fetch failed for %s: %s", league, e)
        return []


async def fetch_sofa_live(sport_slug: str) -> List[Dict]:
    """Fetch live events from SofaScore for a sport."""
    try:
        url = f"{_SOFA_BASE}/sport/{sport_slug}/events/live"
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_SOFA_HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()
        events = r.json().get("events") or []
        results = []
        for e in events[:20]:
            home = (e.get("homeTeam") or {}).get("name", "")
            away = (e.get("awayTeam") or {}).get("name", "")
            hs   = (e.get("homeScore") or {}).get("current", "")
            as_  = (e.get("awayScore") or {}).get("current", "")
            status = (e.get("status") or {}).get("description", "")
            tournament = (e.get("tournament") or {}).get("name", "")
            if home and away:
                results.append({
                    "home": home, "away": away,
                    "home_score": hs, "away_score": as_,
                    "status": status, "tournament": tournament,
                })
        return results
    except Exception as e:
        logger.debug("SofaScore live fetch failed for %s: %s", sport_slug, e)
        return []


async def fetch_sofa_search(query: str) -> List[Dict]:
    """Search SofaScore for teams/events matching a query."""
    try:
        url = f"{_SOFA_BASE}/search/all"
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_SOFA_HEADERS) as client:
            r = await client.get(url, params={"q": query[:60]})
            r.raise_for_status()
        data = r.json()
        results = []
        # Extract team results
        for team in (data.get("teams") or {}).get("results", [])[:3]:
            name    = team.get("name", "")
            country = (team.get("country") or {}).get("name", "")
            if name:
                results.append({"type": "team", "name": name, "country": country})
        # Extract event results
        for event in (data.get("events") or {}).get("results", [])[:5]:
            home  = (event.get("homeTeam") or {}).get("name", "")
            away  = (event.get("awayTeam") or {}).get("name", "")
            hs    = (event.get("homeScore") or {}).get("current", "")
            as_   = (event.get("awayScore") or {}).get("current", "")
            date  = event.get("startTimestamp", "")
            tourney = (event.get("tournament") or {}).get("name", "")
            status  = (event.get("status") or {}).get("description", "")
            if home and away:
                results.append({
                    "type": "event", "home": home, "away": away,
                    "home_score": hs, "away_score": as_,
                    "status": status, "tournament": tourney,
                })
        return results
    except Exception as e:
        logger.debug("SofaScore search failed for '%s': %s", query, e)
        return []


async def fetch_sports_context(title: str) -> Optional[str]:
    """
    High-level: detect league + teams from title, fetch scores + standings,
    return a formatted context string for the AI prompt.
    """
    league = detect_league(title)
    if not league:
        return None

    teams = extract_teams(title)

    import asyncio
    sofa_sport   = _SOFA_SPORTS.get(league, "football")
    # Extract search keywords from title for SofaScore search
    title_words  = " ".join(w for w in title.split() if len(w) > 3)[:60]

    scores, standings, sofa_live, sofa_search = await asyncio.gather(
        fetch_scoreboard(league),
        fetch_standings(league),
        fetch_sofa_live(sofa_sport),
        fetch_sofa_search(title_words),
        return_exceptions=True,
    )

    if isinstance(scores, Exception):    scores = []
    if isinstance(standings, Exception): standings = []
    if isinstance(sofa_live, Exception): sofa_live = []
    if isinstance(sofa_search, Exception): sofa_search = []

    lines = [f"Sports context ({league.upper()}):"]

    # ── ESPN scores ───────────────────────────────────────────────────────────
    if scores:
        relevant = [g for g in scores if g["home_team"] in teams or g["away_team"] in teams]
        shown    = relevant or scores[:5]
        lines.append("  ESPN — Recent/live scores:")
        for g in shown:
            result = "Final" if g["completed"] else g["status"]
            lines.append(
                f"    {g['away_team']} {g['away_score']} @ {g['home_team']} {g['home_score']}  [{result}]"
            )

    # ── SofaScore live events ─────────────────────────────────────────────────
    if sofa_live:
        title_lower = title.lower()
        relevant_live = [
            e for e in sofa_live
            if any(t.lower() in (e["home"] + e["away"]).lower() for t in (teams or [""]))
            or any(kw in (e["home"] + e["away"]).lower() for kw in title_lower.split()[:4])
        ]
        shown_live = relevant_live or sofa_live[:5]
        if shown_live:
            lines.append("  SofaScore — Live now:")
            for e in shown_live[:6]:
                lines.append(
                    f"    {e['away']} {e['away_score']} @ {e['home']} {e['home_score']}"
                    f"  [{e['status']}]  ({e['tournament']})"
                )

    # ── SofaScore search results ──────────────────────────────────────────────
    if sofa_search:
        event_results = [r for r in sofa_search if r.get("type") == "event"]
        if event_results:
            lines.append("  SofaScore — Matching events:")
            for e in event_results[:4]:
                lines.append(
                    f"    {e['away']} {e['away_score']} @ {e['home']} {e['home_score']}"
                    f"  [{e['status']}]  ({e['tournament']})"
                )

    # ── ESPN standings ────────────────────────────────────────────────────────
    if standings and teams:
        relevant_standings = [s for s in standings if s["team"] in teams]
        if relevant_standings:
            lines.append("  ESPN Standings (relevant teams):")
            for s in relevant_standings:
                stat_str = "  ".join(f"{k}:{v}" for k, v in list(s["stats"].items())[:5])
                lines.append(f"    {s['team']}: {stat_str}")
    elif standings and not teams:
        lines.append("  ESPN Standings (top 5):")
        for s in standings[:5]:
            stat_str = "  ".join(f"{k}:{v}" for k, v in list(s["stats"].items())[:3])
            lines.append(f"    {s['team']}: {stat_str}")

    return "\n".join(lines) if len(lines) > 1 else None


def format_sports(context: str) -> str:
    return context
