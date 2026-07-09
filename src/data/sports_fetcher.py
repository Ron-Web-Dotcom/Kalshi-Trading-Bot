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

import asyncio
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

# SofaScore sport slugs — covers every sport SofaScore tracks
_SOFA_SPORTS = {
    # American sports
    "nfl":              "american-football",
    "ncaaf":            "american-football",
    "nba":              "basketball",
    "ncaab":            "basketball",
    "wnba":             "basketball",
    "mlb":              "baseball",
    "nhl":              "ice-hockey",
    "ufc":              "mma",
    "mma":              "mma",
    "boxing":           "boxing",
    # Soccer — all leagues
    "epl":              "football",
    "laliga":           "football",
    "bundesliga":       "football",
    "seriea":           "football",
    "ligue1":           "football",
    "ucl":              "football",
    "uel":              "football",
    "uecl":             "football",
    "mls":              "football",
    "worldcup":         "football",
    "euros":            "football",
    "nwsl":             "football",
    "copalibertadores": "football",
    "concacaf":         "football",
    "soccer":           "football",
    "football":         "football",
    # Tennis
    "tennis":           "tennis",
    "atp":              "tennis",
    "wta":              "tennis",
    "wimbledon":        "tennis",
    "usopen_tennis":    "tennis",
    "ausopen":          "tennis",
    "rolandgarros":     "tennis",
    # Golf
    "golf":             "golf",
    "pga":              "golf",
    "masters":          "golf",
    # Motorsport
    "f1":               "motorsport",
    "formula1":         "motorsport",
    "nascar":           "motorsport",
    # Other
    "rugby":            "rugby",
    "rugbyleague":      "rugby-league",
    "handball":         "handball",
    "volleyball":       "volleyball",
    "cricket":          "cricket",
    "darts":            "darts",
    "snooker":          "snooker",
    "esports":          "esports",
    "cycling":          "cycling",
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
    "cubs":        "CHC",  "mets":       "NYM", "sf giants":  "SF",
    "braves":      "ATL",  "astros":     "HOU", "st. louis cardinals": "STL",
    "phillies":    "PHI",  "blue jays":  "TOR", "padres":     "SD",
    "brewers":     "MIL",  "mariners":   "SEA", "nationals":  "WSH",
    "tigers":      "DET",  "white sox":  "CWS", "twins":      "MIN",
    "athletics":   "OAK",  "angels":     "LAA", "texas rangers": "TEX",
    "royals":      "KC",   "orioles":    "BAL", "pirates":    "PIT",
    "reds":        "CIN",  "rockies":    "COL", "diamondbacks":"ARI",
    "marlins":     "MIA",  "rays":       "TB",
    # NHL
    "bruins":      "BOS",  "canadiens":  "MTL", "maple leafs":"TOR",
    "rangers":     "NYR",  "islanders":  "NYI", "flyers":     "PHI",
    "penguins":    "PIT",  "capitals":   "WSH", "blackhawks": "CHI",
    "red wings":   "DET",  "blues":      "STL", "wild":       "MIN",
    "predators":   "NSH",  "lightning":  "TB",  "florida panthers": "FLA",
    "hurricanes":  "CAR",  "avalanche":  "COL", "golden knights": "VGK",
    "oilers":      "EDM",  "flames":     "CGY", "canucks":    "VAN",
    "sharks":      "SJS",  "ducks":      "ANA", "la kings":   "LAK",
    "coyotes":     "ARI",  "winnipeg jets": "WPG", "senators":   "OTT",

    # Soccer — Premier League (EPL)
    "arsenal":         "ARS",  "chelsea":       "CHE",  "liverpool":    "LIV",
    "manchester city": "MCI",  "man city":      "MCI",  "man utd":      "MUN",
    "manchester united":"MUN", "tottenham":     "TOT",  "tottenham hotspur": "TOT",
    "newcastle":       "NEW",  "aston villa":   "AVL",  "west ham":     "WHU",
    "brighton":        "BHA",  "everton":       "EVE",  "fulham":       "FUL",
    "brentford":       "BRE",  "crystal palace":"CRY",  "wolverhampton":"WOL",
    "wolverhampton wanderers": "WOL", "nottingham":    "NFO",  "leicester":    "LEI",
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
        logger.warning("Scoreboard fetch failed for %s: %s", league, e)
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
        logger.warning("Standings fetch failed for %s: %s", league, e)
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
        logger.warning("SofaScore live fetch failed for %s: %s", sport_slug, e)
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
        logger.warning("SofaScore search failed for '%s': %s", query, e)
        return []


async def _sofa_get(path: str, params: dict = None):
    """Raw SofaScore API GET — returns parsed JSON or None."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_SOFA_HEADERS,
                                     follow_redirects=True) as c:
            r = await c.get(f"{_SOFA_BASE}{path}", params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.debug("SofaScore GET %s: %s", path, e)
        return None


async def sofa_search_player(name: str) -> Optional[int]:
    """Return SofaScore player ID for a player name, or None."""
    data = await _sofa_get("/search/all", {"q": name})
    if not data:
        return None
    for p in (data.get("players") or {}).get("results", []):
        if name.lower() in (p.get("name") or "").lower():
            return p.get("id")
    return None


async def sofa_search_team(name: str) -> Optional[int]:
    """Return SofaScore team ID for a team name, or None."""
    data = await _sofa_get("/search/all", {"q": name})
    if not data:
        return None
    for t in (data.get("teams") or {}).get("results", []):
        if name.lower() in (t.get("name") or "").lower():
            return t.get("id")
    return None


async def sofa_player_stats(player_id: int, tournament_id: int = None) -> Optional[str]:
    """
    Fetch a player's per-game statistics from SofaScore.
    Returns formatted string: 'LeBron James: 28.4 pts | 7.3 reb | 8.1 ast | 48.2 FG%'
    """
    # Get recent seasons
    seasons_data = await _sofa_get(f"/player/{player_id}/statistics/seasons")
    if not seasons_data:
        return None

    seasons = seasons_data.get("seasons") or []
    if not seasons:
        return None

    # Use most recent season
    latest = seasons[0]
    season_id = latest.get("id")
    tournament_id = tournament_id or (latest.get("uniqueTournament") or {}).get("id")
    if not season_id or not tournament_id:
        return None

    stats_data = await _sofa_get(
        f"/player/{player_id}/unique-tournament/{tournament_id}/season/{season_id}/statistics/overall"
    )
    if not stats_data:
        return None

    stats = stats_data.get("statistics") or {}
    if not stats:
        return None

    # Build human-readable stat line — handles every sport SofaScore tracks
    parts = []
    s = stats  # shorthand

    # Universal
    if s.get("rating"):           parts.append(f"Rating {float(s['rating']):.1f}")
    if s.get("appearances"):      parts.append(f"{s['appearances']} games")

    # Basketball (NBA, NCAAB, WNBA)
    if s.get("pointsPerGame")     is not None: parts.append(f"{s['pointsPerGame']:.1f} pts/g")
    if s.get("reboundsPerGame")   is not None: parts.append(f"{s['reboundsPerGame']:.1f} reb/g")
    if s.get("assistsPerGame")    is not None: parts.append(f"{s['assistsPerGame']:.1f} ast/g")
    if s.get("stealsPerGame")     is not None: parts.append(f"{s['stealsPerGame']:.1f} stl/g")
    if s.get("blocksPerGame")     is not None: parts.append(f"{s['blocksPerGame']:.1f} blk/g")
    if s.get("fieldGoalPercentage") is not None: parts.append(f"{s['fieldGoalPercentage']*100:.1f}% FG")
    if s.get("threePointPercentage") is not None: parts.append(f"{s['threePointPercentage']*100:.1f}% 3PT")

    # Soccer / football (all leagues)
    if s.get("goals")             is not None: parts.append(f"{s['goals']} goals")
    if s.get("goalAssists")       is not None: parts.append(f"{s['goalAssists']} assists")
    if s.get("goalsPer90")        is not None: parts.append(f"{s['goalsPer90']:.2f} goals/90")
    if s.get("keyPasses")         is not None: parts.append(f"{s['keyPasses']} key passes")
    if s.get("accuratePasses")    is not None: parts.append(f"{s['accuratePasses']} acc. passes")
    if s.get("yellowCards")       is not None: parts.append(f"{s['yellowCards']}Y")
    if s.get("redCards")          is not None and s['redCards']: parts.append(f"{s['redCards']}R")
    # Goalkeeper
    if s.get("savePercentage")    is not None: parts.append(f"{s['savePercentage']:.1f}% saves")
    if s.get("cleanSheets")       is not None: parts.append(f"{s['cleanSheets']} clean sheets")

    # American Football (NFL, NCAAF)
    if s.get("passingYards")      is not None: parts.append(f"{s['passingYards']} pass yds")
    if s.get("passingTouchdowns") is not None: parts.append(f"{s['passingTouchdowns']} pass TDs")
    if s.get("rushingYards")      is not None: parts.append(f"{s['rushingYards']} rush yds")
    if s.get("rushingTouchdowns") is not None: parts.append(f"{s['rushingTouchdowns']} rush TDs")
    if s.get("receivingYards")    is not None: parts.append(f"{s['receivingYards']} rec yds")
    if s.get("receptions")        is not None: parts.append(f"{s['receptions']} rec")
    if s.get("interceptions")     is not None: parts.append(f"{s['interceptions']} INTs")
    if s.get("sacks")             is not None: parts.append(f"{s['sacks']} sacks")

    # Ice Hockey (NHL)
    if s.get("goals") is not None and s.get("goalAssists") is not None and s.get("passingYards") is None:
        g  = s.get("goals") or 0
        a  = s.get("goalAssists") or 0
        if g or a:
            parts.append(f"{g}G {a}A {g+a}pts (hockey)")
    if s.get("plusMinus")         is not None: parts.append(f"{int(s['plusMinus']):+d} +/-")
    if s.get("penaltyMinutes")    is not None: parts.append(f"{s['penaltyMinutes']} PIM")
    if s.get("shotsOnTarget")     is not None: parts.append(f"{s['shotsOnTarget']} SOG")

    # Baseball (MLB)
    if s.get("battingAverage") is not None:
        avg = s['battingAverage']
        avg_int = int(avg) if avg >= 1 else int(avg * 1000)
        parts.append(f".{avg_int:03d} AVG")
    if s.get("homeRuns")          is not None: parts.append(f"{s['homeRuns']} HR")
    if s.get("runsBattedIn")      is not None: parts.append(f"{s['runsBattedIn']} RBI")
    if s.get("stolenBases")       is not None: parts.append(f"{s['stolenBases']} SB")
    if s.get("onBasePlusSlugging") is not None: parts.append(f"{s['onBasePlusSlugging']:.3f} OPS")
    if s.get("earnedRunAverage")  is not None: parts.append(f"{s['earnedRunAverage']:.2f} ERA")
    if s.get("wins")              is not None: parts.append(f"{s['wins']}W-{s.get('losses',0)}L")
    if s.get("strikeouts")        is not None: parts.append(f"{s['strikeouts']} K")
    if s.get("whip")              is not None: parts.append(f"{s['whip']:.2f} WHIP")

    # Tennis (ATP/WTA)
    if s.get("aces")              is not None: parts.append(f"{s['aces']} aces")
    if s.get("doubleFaults")      is not None: parts.append(f"{s['doubleFaults']} DFs")
    if s.get("firstServePercentage") is not None: parts.append(f"{s['firstServePercentage']:.1f}% 1st serve")
    if s.get("breakPointsConverted") is not None: parts.append(f"{s['breakPointsConverted']} BP conv.")

    # Golf (PGA)
    if s.get("scoringAverage")    is not None: parts.append(f"{s['scoringAverage']:.2f} scoring avg")
    if s.get("drivingDistance")   is not None: parts.append(f"{s['drivingDistance']:.1f} yds driving")

    # MMA / Boxing
    if s.get("wins")              is not None and s.get("earnedRunAverage") is None:
        parts.append(f"{s.get('wins',0)}W-{s.get('losses',0)}L-{s.get('draws',0)}D")
    if s.get("knockouts")         is not None: parts.append(f"{s['knockouts']} KOs")
    if s.get("submissions")       is not None: parts.append(f"{s['submissions']} subs")

    # Cricket
    if s.get("runs")              is not None: parts.append(f"{s['runs']} runs")
    if s.get("wickets")           is not None: parts.append(f"{s['wickets']} wickets")
    if s.get("battingAverage")    is None and s.get("average") is not None:
        parts.append(f"{s['average']:.2f} avg")

    # Rugby
    if s.get("tries")             is not None: parts.append(f"{s['tries']} tries")
    if s.get("tackles")           is not None: parts.append(f"{s['tackles']} tackles")

    if not parts:
        return None

    season_name = latest.get("year") or latest.get("name") or "current season"
    return f"{season_name}: {' | '.join(parts)}"


async def sofa_player_last_games(player_id: int, n: int = 5) -> Optional[str]:
    """
    Fetch a player's last N game performances from SofaScore.
    Returns formatted string with recent form.
    """
    data = await _sofa_get(f"/player/{player_id}/events/last/0")
    if not data:
        return None

    events = (data.get("events") or [])[:n]
    if not events:
        return None

    lines = []
    for ev in events:
        home   = (ev.get("homeTeam") or {}).get("name", "?")
        away   = (ev.get("awayTeam") or {}).get("name", "?")
        hs     = (ev.get("homeScore") or {}).get("current", "?")
        as_    = (ev.get("awayScore") or {}).get("current", "?")
        status = (ev.get("status") or {}).get("description", "")
        lines.append(f"  {home} {hs}–{as_} {away} ({status})")

    return f"Last {len(lines)} games:\n" + "\n".join(lines)


async def sofa_h2h(team1_id: int, team2_id: int) -> Optional[str]:
    """
    Fetch head-to-head record between two teams from SofaScore.
    Returns formatted summary: 'Last 5 H2H: Team A 3W 1D 1L'
    """
    data = await _sofa_get(f"/team/{team1_id}/near-events/{team2_id}")
    if not data:
        # Try events endpoint
        data = await _sofa_get(f"/event/{team1_id}/h2h/{team2_id}")
    if not data:
        return None

    events = (data.get("teamDuel") or {}).get("managerDuel", {})
    team1_wins = events.get("wins1") or 0
    team2_wins = events.get("wins2") or 0
    draws      = events.get("draws")  or 0
    total      = team1_wins + team2_wins + draws
    if not total:
        return None

    return f"H2H (last {total}): {team1_wins}W {draws}D {team2_wins}L"


async def sofa_team_recent_form(team_id: int, n: int = 5) -> Optional[str]:
    """
    Fetch team's last N results from SofaScore.
    Returns: 'Recent form: W W L W D (last 5)'
    """
    data = await _sofa_get(f"/team/{team_id}/events/last/0")
    if not data:
        return None

    events = (data.get("events") or [])[:n]
    if not events:
        return None

    form = []
    for ev in events:
        home_id = (ev.get("homeTeam") or {}).get("id")
        hs = int((ev.get("homeScore") or {}).get("current") or 0)
        as_ = int((ev.get("awayScore") or {}).get("current") or 0)
        if home_id == team_id:
            form.append("W" if hs > as_ else "D" if hs == as_ else "L")
        else:
            form.append("W" if as_ > hs else "D" if hs == as_ else "L")

    return f"Recent form: {' '.join(form)} (last {len(form)})"


async def fetch_sofa_deep_context(title: str) -> Optional[str]:
    """
    Full deep SofaScore research for ANY sport in a market title.

    Covers: NBA, NFL, MLB, NHL, NCAA, soccer (all leagues), tennis (ATP/WTA),
    golf (PGA), MMA/UFC, boxing, rugby, cricket, handball, volleyball,
    motorsport (F1/NASCAR), esports, cycling, darts, snooker — everything
    SofaScore tracks.

    Runs in parallel:
      1. Detect sport from title → fetch live events for that sport
      2. Search for player names → per-game season stats + last 5 games
      3. Search for team names → recent form
      4. H2H between two teams if both found
    """
    import re as _re

    t_lower = title.lower()

    # Detect sport slug from title keywords
    _SPORT_KEYWORDS: List[Tuple[str, str]] = [
        # American sports
        ("nfl",          "american-football"),
        ("super bowl",   "american-football"),
        ("quarterback",  "american-football"),
        ("touchdown",    "american-football"),
        ("nba",          "basketball"),
        ("wnba",         "basketball"),
        ("basketball",   "basketball"),
        ("march madness","basketball"),
        ("final four",   "basketball"),
        ("mlb",          "baseball"),
        ("baseball",     "baseball"),
        ("world series", "baseball"),
        ("nhl",          "ice-hockey"),
        ("hockey",       "ice-hockey"),
        ("stanley cup",  "ice-hockey"),
        ("ufc",          "mma"),
        ("mma",          "mma"),
        ("boxing",       "boxing"),
        # Soccer / football
        ("premier league","football"),
        ("epl",           "football"),
        ("laliga",        "football"),
        ("bundesliga",    "football"),
        ("serie a",       "football"),
        ("ligue 1",       "football"),
        ("champions league","football"),
        ("europa league", "football"),
        ("world cup",     "football"),
        ("euros",         "football"),
        ("mls",           "football"),
        ("soccer",        "football"),
        # Tennis
        ("tennis",        "tennis"),
        ("atp",           "tennis"),
        ("wta",           "tennis"),
        ("wimbledon",     "tennis"),
        ("us open",       "tennis"),
        ("french open",   "tennis"),
        ("australian open","tennis"),
        ("roland garros", "tennis"),
        # Golf
        ("golf",          "golf"),
        ("pga",           "golf"),
        ("masters",       "golf"),
        ("open championship","golf"),
        # Motorsport
        ("formula 1",     "motorsport"),
        ("f1",            "motorsport"),
        ("grand prix",    "motorsport"),
        ("nascar",        "motorsport"),
        # Other
        ("rugby",         "rugby"),
        ("cricket",       "cricket"),
        ("handball",      "handball"),
        ("volleyball",    "volleyball"),
        ("cycling",       "cycling"),
        ("tour de france","cycling"),
        ("darts",         "darts"),
        ("snooker",       "snooker"),
        ("esports",       "esports"),
    ]

    detected_slug = "football"  # default to soccer (most global)
    for kw, slug in _SPORT_KEYWORDS:
        if kw in t_lower:
            detected_slug = slug
            break

    # Extract player names (FirstName LastName) and team/entity names
    players = _re.findall(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", title)
    teams   = _re.findall(r"\b(?:the\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", title)
    teams   = [t for t in teams if len(t) > 4 and t not in players][:3]

    if not players and not teams:
        return None

    blocks = []

    # ── Live events for this sport ────────────────────────────────────────────
    live_events = await fetch_sofa_live(detected_slug)
    if live_events:
        live_lines = []
        for ev in live_events[:4]:
            home  = ev.get("home", "?")
            away  = ev.get("away", "?")
            hs    = ev.get("home_score", "?")
            as_   = ev.get("away_score", "?")
            live_lines.append(f"  🔴 {home} {hs}–{as_} {away} (LIVE)")
        if live_lines:
            blocks.append("Live now on SofaScore:\n" + "\n".join(live_lines))

    # ── Player stats ─────────────────────────────────────────────────────────
    for player_name in players[:3]:
        pid = await sofa_search_player(player_name)
        if not pid:
            continue

        season_stats, last_games = await asyncio.gather(
            sofa_player_stats(pid),
            sofa_player_last_games(pid, n=5),
            return_exceptions=True,
        )

        player_lines = [f"📊 **{player_name}** (SofaScore)"]
        if isinstance(season_stats, str) and season_stats:
            player_lines.append(f"  Season: {season_stats}")
        if isinstance(last_games, str) and last_games:
            player_lines.append(f"  {last_games}")
        if len(player_lines) > 1:
            blocks.append("\n".join(player_lines))

    # ── Team form + H2H ──────────────────────────────────────────────────────
    team_ids: List[Tuple[str, int]] = []
    for team_name in teams[:2]:
        tid = await sofa_search_team(team_name)
        if tid:
            team_ids.append((team_name, tid))

    for team_name, tid in team_ids:
        form = await sofa_team_recent_form(tid)
        if form:
            blocks.append(f"🏟 **{team_name}**: {form}")

    if len(team_ids) >= 2:
        h2h = await sofa_h2h(team_ids[0][1], team_ids[1][1])
        if h2h:
            blocks.append(f"⚔️ {team_ids[0][0]} vs {team_ids[1][0]}: {h2h}")

    if not blocks:
        return None

    sport_label = detected_slug.replace("-", " ").title()
    return f"=== SOFASCORE DEEP STATS ({sport_label}) ===\n" + "\n\n".join(blocks)


async def fetch_statmuse(query: str, sport: str = "") -> Optional[str]:
    """
    StatMuse — natural language sports stats, game results, player records.
    Scrapes statmuse.com/search?q=... for the answer snippet.

    Returns a one-liner like:
      "The Knicks are 3-1 in the 2026 NBA Finals."
      "LeBron James scored 32 points in Game 4."
      "The Hurricanes lost Game 5 4-2."
    or None if no useful result found.
    """
    try:
        from urllib.parse import quote_plus
        q = query.strip()[:120]
        url = f"https://www.statmuse.com/search?q={quote_plus(q)}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.statmuse.com/",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers,
                                     follow_redirects=True) as client:
            r = await client.get(url)
            html = r.text

        # StatMuse returns the answer in an <h1> or a .nlg-answer / .answer span
        import re as _re
        # Try structured answer first
        for pattern in [
            r'<h1[^>]*class="[^"]*nlg[^"]*"[^>]*>([^<]{10,300})</h1>',
            r'<p[^>]*class="[^"]*nlg[^"]*"[^>]*>([^<]{10,300})</p>',
            r'<span[^>]*class="[^"]*answer[^"]*"[^>]*>([^<]{10,300})</span>',
            r'<h1[^>]*>([^<]{15,300})</h1>',
        ]:
            m = _re.search(pattern, html, _re.IGNORECASE)
            if m:
                text = m.group(1).strip()
                text = _re.sub(r'\s+', ' ', text)
                if len(text) > 10 and not text.startswith("StatMuse"):
                    logger.debug("StatMuse answer: %s", text[:100])
                    return f"StatMuse: {text}"

        return None
    except Exception as e:
        logger.warning("StatMuse fetch failed for '%s': %s", query, e)
        return None


async def fetch_statmuse_player_context(title: str) -> Optional[str]:
    """
    Pull deep player history from StatMuse — per-game averages, career stats,
    recent form, head-to-head records.

    Runs multiple natural language queries in parallel:
      - Points/goals per game this season
      - Career averages
      - Last 5 games performance
      - Head-to-head vs opponent (if two teams detected)
      - Player injury/status signals

    Returns a formatted context block or None.
    """
    import re as _re

    # Extract player names — capitalized words that look like names
    players = _re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", title)
    # Extract team names
    teams   = _re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", title)

    # Build targeted queries StatMuse handles best
    queries = []
    for player in players[:2]:
        queries += [
            f"how many points does {player} average per game this season",
            f"{player} stats last 5 games",
            f"{player} career averages per game",
        ]

    # Team H2H if two teams detected
    if len(teams) >= 2:
        queries.append(f"{teams[0]} vs {teams[1]} last 5 games results")
        queries.append(f"{teams[0]} record this season")

    # Fallback — use the market title directly as a natural language query
    if not queries:
        clean = title.replace("Will ","").replace("?","").replace(" win ","").strip()
        queries = [
            f"{clean} stats this season",
            f"{clean} average per game",
        ]

    # Run all queries in parallel, cap at 5
    results = await asyncio.gather(
        *[fetch_statmuse(q) for q in queries[:5]],
        return_exceptions=True,
    )

    lines = []
    for q, r in zip(queries[:5], results):
        if isinstance(r, str) and r:
            lines.append(f"Q: {q}\n→ {r}")

    if not lines:
        return None

    return "=== STATMUSE PLAYER & TEAM HISTORY ===\n" + "\n\n".join(lines)


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


# is_event_live_now moved to src/data/live_event_detector.py (multi-category)
