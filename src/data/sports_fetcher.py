"""
Sports data via ESPN's public (unofficial) JSON API — no API key required.

Fetches live/recent scores, standings, and team records to inform AI
decisions on Kalshi sports markets:
  "Will the Chiefs win Super Bowl LX?"
  "Will the Lakers make the NBA playoffs?"
  "Will the Yankees win more than 90 games?"

Endpoints used (all public, no auth):
  https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard
  https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/standings
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("trading.sports_fetcher")

_TIMEOUT = httpx.Timeout(10.0)
_BASE    = "https://site.api.espn.com/apis/site/v2/sports"

# Supported leagues with ESPN sport/league path
LEAGUES: Dict[str, Tuple[str, str]] = {
    "nfl":    ("football",   "nfl"),
    "nba":    ("basketball", "nba"),
    "mlb":    ("baseball",   "mlb"),
    "nhl":    ("hockey",     "nhl"),
    "ncaaf":  ("football",   "college-football"),
    "ncaab":  ("basketball", "mens-college-basketball"),
    "mls":    ("soccer",     "usa.1"),
    "epl":    ("soccer",     "eng.1"),
    "ufc":    ("mma",        "ufc"),
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
}

# League detection patterns
_LEAGUE_PATTERNS: Dict[str, List[str]] = {
    "nfl":   ["nfl", "super bowl", "football", "touchdown", "quarterback"],
    "nba":   ["nba", "basketball", "playoff", "finals"],
    "mlb":   ["mlb", "baseball", "world series", "innings", "batting"],
    "nhl":   ["nhl", "hockey", "stanley cup", "goalie", "puck"],
    "ncaaf": ["ncaa football", "college football", "cfp", "bowl game"],
    "ncaab": ["ncaa basketball", "march madness", "final four"],
    "mls":   ["mls", "soccer", "major league soccer"],
}


def detect_league(title: str) -> Optional[str]:
    """Detect which sports league a market title refers to."""
    t = title.lower()
    for league, patterns in _LEAGUE_PATTERNS.items():
        if any(p in t for p in patterns):
            return league
    # Check team aliases as fallback
    for alias in TEAM_ALIASES:
        if re.search(r'\b' + re.escape(alias) + r'\b', t):
            if alias in ["lakers", "celtics", "warriors", "heat", "bulls", "knicks",
                         "nets", "76ers", "suns", "nuggets", "bucks", "clippers",
                         "spurs", "mavericks", "mavs", "rockets", "thunder", "jazz",
                         "blazers", "kings", "pelicans", "grizzlies", "hawks", "hornets",
                         "magic", "pistons", "cavaliers", "cavs", "raptors", "wolves",
                         "timberwolves", "pacers"]:
                return "nba"
            if alias in ["yankees", "red sox", "dodgers", "cubs", "mets", "braves",
                         "astros", "phillies", "blue jays", "padres", "brewers",
                         "mariners", "nationals", "tigers", "white sox", "twins",
                         "athletics", "angels", "rangers", "royals", "orioles",
                         "pirates", "reds", "rockies", "diamondbacks", "marlins", "rays"]:
                return "mlb"
            if alias in ["bruins", "canadiens", "maple leafs", "rangers", "islanders",
                         "flyers", "penguins", "capitals", "blackhawks", "red wings",
                         "blues", "wild", "predators", "lightning", "hurricanes",
                         "avalanche", "golden knights", "oilers", "flames", "canucks",
                         "sharks", "ducks", "kings", "coyotes", "jets", "senators"]:
                return "nhl"
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
    scores_task    = fetch_scoreboard(league)
    standings_task = fetch_standings(league)
    scores, standings = await asyncio.gather(scores_task, standings_task, return_exceptions=True)

    if isinstance(scores, Exception):
        scores = []
    if isinstance(standings, Exception):
        standings = []

    lines = [f"Sports context ({league.upper()}):"]

    # Filter scores to only games involving our teams (or show last 5 if no match)
    if scores:
        relevant = [g for g in scores if g["home_team"] in teams or g["away_team"] in teams]
        shown    = relevant or scores[:5]
        lines.append("  Recent/live scores:")
        for g in shown:
            status = g["status"]
            if g["completed"]:
                lines.append(
                    f"    {g['away_team']} {g['away_score']} @ {g['home_team']} {g['home_score']}  [Final]"
                )
            else:
                lines.append(
                    f"    {g['away_team']} {g['away_score']} @ {g['home_team']} {g['home_score']}  [{status}]"
                )

    # Filter standings to teams we care about
    if standings and teams:
        relevant_standings = [s for s in standings if s["team"] in teams]
        if relevant_standings:
            lines.append("  Standings for relevant teams:")
            for s in relevant_standings:
                stat_str = "  ".join(
                    f"{k}:{v}" for k, v in list(s["stats"].items())[:5]
                )
                lines.append(f"    {s['team']}: {stat_str}")
    elif standings and not teams:
        lines.append("  League standings (top 5):")
        for s in standings[:5]:
            stat_str = "  ".join(
                f"{k}:{v}" for k, v in list(s["stats"].items())[:3]
            )
            lines.append(f"    {s['team']}: {stat_str}")

    return "\n".join(lines) if len(lines) > 1 else None


def format_sports(context: str) -> str:
    return context
