"""
Multi-source sports data aggregator — fast parallel fetching from:
  1. ESPN API         — live scores, standings, injuries, stats, depth charts
  2. TheSportsDB      — historical results, season stats, team details
  3. Ball Don't Lie   — NBA player/team game stats
  4. MLB Stats API    — official MLB game logs and stats
  5. Injury RSS       — Rotoworld + CBS Sports injury feeds
  6. ESPN Headlines   — sport-specific news feeds

All sources run in parallel. Returns a rich formatted context string
for the AI prompt — more context = higher AI confidence = more bids placed.
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger("trading.sports_data")

_TIMEOUT = httpx.Timeout(8.0)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/html, */*",
}

# ── ESPN endpoints ─────────────────────────────────────────────────────────────

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_ESPN_CORE = "https://sports.core.api.espn.com/v2/sports"

# sport/league → (sport_slug, league_slug, display_name)
ESPN_LEAGUES: Dict[str, Tuple[str, str, str]] = {
    "nfl":       ("football",   "nfl",                    "NFL"),
    "nba":       ("basketball", "nba",                    "NBA"),
    "mlb":       ("baseball",   "mlb",                    "MLB"),
    "nhl":       ("hockey",     "nhl",                    "NHL"),
    "ncaaf":     ("football",   "college-football",       "NCAA Football"),
    "ncaab":     ("basketball", "mens-college-basketball","NCAA Basketball"),
    "ufc":       ("mma",        "ufc",                    "UFC"),
    "mls":       ("soccer",     "usa.1",                  "MLS"),
    "nwsl":      ("soccer",     "usa.nwsl",               "NWSL"),
    "epl":       ("soccer",     "eng.1",                  "Premier League"),
    "championship": ("soccer",  "eng.2",                  "Championship"),
    "laliga":    ("soccer",     "esp.1",                  "La Liga"),
    "bundesliga": ("soccer",    "ger.1",                  "Bundesliga"),
    "seriea":    ("soccer",     "ita.1",                  "Serie A"),
    "ligue1":    ("soccer",     "fra.1",                  "Ligue 1"),
    "eredivisie": ("soccer",    "ned.1",                  "Eredivisie"),
    "ucl":       ("soccer",     "uefa.champions",         "Champions League"),
    "uel":       ("soccer",     "uefa.europa",            "Europa League"),
    "uecl":      ("soccer",     "uefa.europa.conf",       "Conference League"),
    "worldcup":  ("soccer",     "fifa.world",             "FIFA World Cup"),
    "euros":     ("soccer",     "uefa.euro",              "UEFA Euros"),
    "copalibertadores": ("soccer", "conmebol.libertadores", "Copa Libertadores"),
    "concacaf":  ("soccer",     "concacaf.champions",     "CONCACAF Champions"),
    "brasileirao": ("soccer",   "bra.1",                  "Brasileirão"),
    "primeiraliga": ("soccer",  "por.1",                  "Primeira Liga"),
    "scottishprem": ("soccer",  "sco.1",                  "Scottish Premiership"),
}

# ESPN news RSS by sport
ESPN_NEWS_RSS = {
    "nfl":    "https://www.espn.com/espn/rss/nfl/news",
    "nba":    "https://www.espn.com/espn/rss/nba/news",
    "mlb":    "https://www.espn.com/espn/rss/mlb/news",
    "nhl":    "https://www.espn.com/espn/rss/nhl/news",
    "soccer": "https://www.espn.com/espn/rss/soccer/news",
    "ncaaf":  "https://www.espn.com/espn/rss/ncf/news",
    "ncaab":  "https://www.espn.com/espn/rss/ncb/news",
    "ufc":    "https://www.espn.com/espn/rss/mma/news",
}

# TheSportsDB (free tier, no API key)
TSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"

# Ball Don't Lie — NBA
BDL_BASE = "https://api.balldontlie.io/v1"

# MLB Stats API — official
MLB_BASE = "https://statsapi.mlb.com/api/v1"


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_json(url: str, params: dict = None) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS,
                                     follow_redirects=True) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.debug("_get_json %s: %s", url[:70], e)
        return None


async def _get_text(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS,
                                     follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text
    except Exception as e:
        logger.debug("_get_text %s: %s", url[:70], e)
        return None


def _rss_headlines(xml_text: str, max_items: int = 6) -> List[str]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
        out = []
        for item in root.iter("item"):
            t = (item.findtext("title") or "").strip()
            if t and len(t) > 10:
                out.append(t)
            if len(out) >= max_items:
                break
        return out
    except Exception:
        return []


# ── ESPN fetchers ──────────────────────────────────────────────────────────────

async def fetch_espn_scoreboard(league: str) -> List[Dict]:
    """ESPN scoreboard — live/recent scores with status and team names."""
    cfg = ESPN_LEAGUES.get(league)
    if not cfg:
        return []
    sport, league_path, display = cfg
    url = f"{_ESPN_BASE}/{sport}/{league_path}/scoreboard"
    data = await _get_json(url)
    if not data:
        return []
    games = []
    for event in (data.get("events") or [])[:10]:
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        if len(competitors) < 2:
            continue
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
        status = (event.get("status") or {}).get("type") or {}
        games.append({
            "home_team":   (home.get("team") or {}).get("displayName", ""),
            "home_abbr":   (home.get("team") or {}).get("abbreviation", ""),
            "home_score":  home.get("score", ""),
            "home_record": (home.get("records") or [{}])[0].get("summary", ""),
            "away_team":   (away.get("team") or {}).get("displayName", ""),
            "away_abbr":   (away.get("team") or {}).get("abbreviation", ""),
            "away_score":  away.get("score", ""),
            "away_record": (away.get("records") or [{}])[0].get("summary", ""),
            "status":      status.get("description", ""),
            "in_progress": status.get("inProgress", False),
            "completed":   status.get("completed", False),
            "venue":       (comp.get("venue") or {}).get("fullName", ""),
        })
    return games


async def fetch_espn_standings(league: str) -> List[Dict]:
    """ESPN standings — team win/loss records and rank."""
    cfg = ESPN_LEAGUES.get(league)
    if not cfg:
        return []
    sport, league_path, _ = cfg
    url = f"{_ESPN_BASE}/{sport}/{league_path}/standings"
    data = await _get_json(url)
    if not data:
        return []
    entries = []
    for group in (data.get("children") or [data]):
        for standing in (group.get("standings") or {}).get("entries") or []:
            team = (standing.get("team") or {}).get("abbreviation", "")
            name = (standing.get("team") or {}).get("displayName", team)
            stats = {s["name"]: s.get("displayValue", "") for s in (standing.get("stats") or [])}
            if team:
                entries.append({"team": team, "name": name, "stats": stats})
    return entries[:20]


async def fetch_espn_injuries(league: str) -> List[Dict]:
    """ESPN injuries feed — player injury status per team."""
    cfg = ESPN_LEAGUES.get(league)
    if not cfg:
        return []
    sport, league_path, _ = cfg
    url = f"{_ESPN_BASE}/{sport}/{league_path}/injuries"
    data = await _get_json(url)
    if not data:
        return []
    injuries = []
    for item in (data.get("items") or [])[:30]:
        player = (item.get("athlete") or {}).get("displayName", "")
        team   = (item.get("team") or {}).get("abbreviation", "")
        status = (item.get("injuries") or [{}])[0].get("status", "")
        detail = (item.get("injuries") or [{}])[0].get("longComment", "")
        if player and status:
            injuries.append({"player": player, "team": team, "status": status,
                             "detail": detail[:80]})
    return injuries


async def fetch_espn_team_stats(league: str, team_abbr: str) -> Optional[Dict]:
    """ESPN team stats — season stats for a specific team."""
    cfg = ESPN_LEAGUES.get(league)
    if not cfg:
        return None
    sport, league_path, _ = cfg
    # First find team id
    url = f"{_ESPN_BASE}/{sport}/{league_path}/teams"
    data = await _get_json(url)
    if not data:
        return None
    team_id = None
    for item in (data.get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", []):
        t = item.get("team", {})
        if t.get("abbreviation", "").upper() == team_abbr.upper():
            team_id = t.get("id")
            break
    if not team_id:
        return None
    stats_url = f"{_ESPN_BASE}/{sport}/{league_path}/teams/{team_id}/statistics"
    return await _get_json(stats_url)


async def fetch_espn_news(league: str, keywords: List[str]) -> List[str]:
    """ESPN sport-specific RSS news filtered by keywords."""
    # Determine sport key for RSS
    cfg = ESPN_LEAGUES.get(league)
    if not cfg:
        return []
    sport, _, _ = cfg
    rss_key = league if league in ESPN_NEWS_RSS else sport
    rss_url = ESPN_NEWS_RSS.get(rss_key)
    if not rss_url:
        return []
    xml = await _get_text(rss_url)
    headlines = _rss_headlines(xml, 15)
    if not keywords:
        return headlines[:6]
    kws = [k.lower() for k in keywords if len(k) >= 3]
    relevant = [h for h in headlines if any(k in h.lower() for k in kws)]
    return (relevant or headlines)[:6]


async def fetch_espn_recent_results(league: str, team_abbr: str) -> List[Dict]:
    """ESPN last 5 game results for a specific team."""
    cfg = ESPN_LEAGUES.get(league)
    if not cfg:
        return []
    sport, league_path, _ = cfg
    # Use team schedule endpoint
    url = f"{_ESPN_BASE}/{sport}/{league_path}/teams"
    data = await _get_json(url)
    if not data:
        return []
    team_id = None
    for item in ((data.get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams") or []):
        t = item.get("team", {})
        if t.get("abbreviation", "").upper() == team_abbr.upper():
            team_id = t.get("id")
            break
    if not team_id:
        return []
    sched_url = f"{_ESPN_BASE}/{sport}/{league_path}/teams/{team_id}/schedule"
    sched = await _get_json(sched_url)
    if not sched:
        return []
    results = []
    for event in (sched.get("events") or [])[-10:]:
        status = (event.get("competitions") or [{}])[0].get("status") or {}
        if not (status.get("type") or {}).get("completed", False):
            continue
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        if len(competitors) < 2:
            continue
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
        results.append({
            "date":       event.get("date", "")[:10],
            "home_team":  (home.get("team") or {}).get("abbreviation", ""),
            "home_score": home.get("score", ""),
            "away_team":  (away.get("team") or {}).get("abbreviation", ""),
            "away_score": away.get("score", ""),
            "winner":     (home if home.get("winner") else away).get("team", {}).get("abbreviation", ""),
        })
    return results[-5:]


# ── TheSportsDB ────────────────────────────────────────────────────────────────

_TSDB_LEAGUE_IDS = {
    "epl": "4328", "laliga": "4335", "bundesliga": "4331", "seriea": "4332",
    "ligue1": "4334", "mls": "4346", "ucl": "4480", "worldcup": "4347",
    "nfl": "4391", "nba": "4387", "mlb": "4424", "nhl": "4380",
    "ncaaf": "4479", "ufc": "4443",
}

async def fetch_tsdb_last_results(team_name: str) -> List[Dict]:
    """TheSportsDB last 5 results for a team by name."""
    data = await _get_json(f"{TSDB_BASE}/searchteams.php", {"t": team_name[:40]})
    if not data or not data.get("teams"):
        return []
    team_id = data["teams"][0].get("idTeam")
    if not team_id:
        return []
    results_data = await _get_json(f"{TSDB_BASE}/eventslast.php", {"id": team_id})
    if not results_data:
        return []
    games = []
    for e in (results_data.get("results") or [])[:5]:
        games.append({
            "date":       e.get("dateEvent", ""),
            "home_team":  e.get("strHomeTeam", ""),
            "home_score": e.get("intHomeScore", ""),
            "away_team":  e.get("strAwayTeam", ""),
            "away_score": e.get("intAwayScore", ""),
            "league":     e.get("strLeague", ""),
        })
    return games


async def fetch_tsdb_next_event(team_name: str) -> Optional[Dict]:
    """TheSportsDB next scheduled event for a team."""
    data = await _get_json(f"{TSDB_BASE}/searchteams.php", {"t": team_name[:40]})
    if not data or not data.get("teams"):
        return None
    team_id = data["teams"][0].get("idTeam")
    if not team_id:
        return None
    next_data = await _get_json(f"{TSDB_BASE}/eventsnext.php", {"id": team_id})
    if not next_data or not next_data.get("events"):
        return None
    e = next_data["events"][0]
    return {
        "date":      e.get("dateEvent", ""),
        "time":      e.get("strTime", ""),
        "home_team": e.get("strHomeTeam", ""),
        "away_team": e.get("strAwayTeam", ""),
        "league":    e.get("strLeague", ""),
        "venue":     e.get("strVenue", ""),
    }


async def fetch_tsdb_season_table(league: str) -> List[Dict]:
    """TheSportsDB league table/standings for current season."""
    league_id = _TSDB_LEAGUE_IDS.get(league)
    if not league_id:
        return []
    from datetime import datetime
    season = datetime.now().year
    # Try current and previous season
    for s in [season, season - 1]:
        season_str = f"{s}-{s+1}" if league not in ("nfl","nba","mlb","nhl","ufc") else str(s)
        data = await _get_json(
            f"{TSDB_BASE}/lookuptable.php",
            {"l": league_id, "s": season_str},
        )
        table = (data or {}).get("table") or []
        if table:
            return [
                {
                    "pos": t.get("intRank", ""),
                    "team": t.get("strTeam", ""),
                    "played": t.get("intPlayed", ""),
                    "won": t.get("intWin", ""),
                    "drawn": t.get("intDraw", ""),
                    "lost": t.get("intLoss", ""),
                    "points": t.get("intPoints", ""),
                    "goal_diff": t.get("intGoalDifference", ""),
                }
                for t in table[:20]
            ]
    return []


# ── Ball Don't Lie — NBA ───────────────────────────────────────────────────────

async def fetch_nba_game_stats(team_name: str) -> Optional[str]:
    """Ball Don't Lie — NBA team recent game stats."""
    # Search for team
    data = await _get_json(f"{BDL_BASE}/teams", params={"search": team_name[:30]})
    if not data or not data.get("data"):
        return None
    team_id = data["data"][0].get("id")
    if not team_id:
        return None
    # Recent games
    from datetime import datetime, timedelta
    today = datetime.utcnow().strftime("%Y-%m-%d")
    past  = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    games = await _get_json(
        f"{BDL_BASE}/games",
        params={"team_ids[]": team_id, "start_date": past,
                "end_date": today, "per_page": 10},
    )
    if not games or not games.get("data"):
        return None
    lines = [f"NBA recent games ({team_name}):"]
    for g in games["data"][-5:]:
        ht = g.get("home_team", {}).get("abbreviation", "")
        at = g.get("visitor_team", {}).get("abbreviation", "")
        hs = g.get("home_team_score", "")
        as_ = g.get("visitor_team_score", "")
        date = g.get("date", "")[:10]
        status = g.get("status", "")
        lines.append(f"  {date}: {at} {as_} @ {ht} {hs}  [{status}]")
    return "\n".join(lines)


# ── MLB Stats API ──────────────────────────────────────────────────────────────

async def fetch_mlb_schedule(team_id: int = None) -> List[Dict]:
    """Official MLB Stats API — today's games or team schedule."""
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    params = {"sportId": 1, "date": today, "hydrate": "team,linescore"}
    if team_id:
        params["teamId"] = team_id
    data = await _get_json(f"{MLB_BASE}/schedule", params=params)
    if not data:
        return []
    games = []
    for date_entry in (data.get("dates") or []):
        for game in (date_entry.get("games") or []):
            home = game.get("teams", {}).get("home", {})
            away = game.get("teams", {}).get("away", {})
            games.append({
                "date":       date_entry.get("date", ""),
                "home_team":  home.get("team", {}).get("name", ""),
                "home_score": home.get("score", ""),
                "away_team":  away.get("team", {}).get("name", ""),
                "away_score": away.get("score", ""),
                "status":     game.get("status", {}).get("detailedState", ""),
                "venue":      game.get("venue", {}).get("name", ""),
            })
    return games[:10]


# ── Injury feeds (RSS) ─────────────────────────────────────────────────────────

_INJURY_RSS = {
    "nfl":  "https://www.cbssports.com/rss/headlines/nfl/injuries/",
    "nba":  "https://www.cbssports.com/rss/headlines/nba/injuries/",
    "mlb":  "https://www.cbssports.com/rss/headlines/mlb/injuries/",
    "nhl":  "https://www.cbssports.com/rss/headlines/nhl/injuries/",
    "soccer": "https://www.cbssports.com/rss/headlines/soccer/",
}


async def fetch_injury_news(league: str, team_keywords: List[str]) -> List[str]:
    """CBS Sports injury RSS filtered to relevant team keywords."""
    sport_key = league if league in _INJURY_RSS else "nfl"
    url = _INJURY_RSS.get(sport_key)
    if not url:
        return []
    xml = await _get_text(url)
    headlines = _rss_headlines(xml, 20)
    if not team_keywords:
        return headlines[:5]
    kws = [k.lower() for k in team_keywords if len(k) >= 3]
    relevant = [h for h in headlines if any(k in h.lower() for k in kws)]
    return (relevant or headlines[:3])[:5]


# ── Main entry point ───────────────────────────────────────────────────────────

async def fetch_comprehensive_sports_context(
    title: str,
    league: str,
    teams: List[str],
    team_names: List[str],
    timeout: float = 9.0,
) -> str:
    """
    Fetch rich multi-source sports context for a market title.
    Runs ESPN, TheSportsDB, Ball Don't Lie, MLB Stats, injury feeds in parallel.
    Returns formatted string for AI prompt injection.
    """
    cfg = ESPN_LEAGUES.get(league)
    display_name = cfg[2] if cfg else league.upper()

    # Build keyword list for filtering
    kws = list(set(teams + team_names + [w for w in title.split() if len(w) >= 4]))[:10]

    # Determine which sources apply
    is_nba   = league == "nba"
    is_mlb   = league == "mlb"
    is_soccer = cfg and cfg[0] == "soccer" if cfg else False

    tasks = {
        "scoreboard": fetch_espn_scoreboard(league),
        "standings":  fetch_espn_standings(league),
        "espn_news":  fetch_espn_news(league, kws),
        "injuries":   fetch_injury_news(league, kws),
    }

    if is_nba and team_names:
        tasks["nba_stats"] = fetch_nba_game_stats(team_names[0])
    if is_mlb:
        tasks["mlb_today"] = fetch_mlb_schedule()
    if is_soccer and team_names:
        tasks["tsdb_table"] = fetch_tsdb_season_table(league)
        tasks["tsdb_last"]  = fetch_tsdb_last_results(team_names[0])
        tasks["tsdb_next"]  = fetch_tsdb_next_event(team_names[0])
    elif team_names:
        tasks["tsdb_last"]  = fetch_tsdb_last_results(team_names[0])
        tasks["tsdb_next"]  = fetch_tsdb_next_event(team_names[0])

    try:
        gathered = await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True),
            timeout=timeout,
        )
        results = dict(zip(tasks.keys(), gathered))
    except asyncio.TimeoutError:
        logger.warning("Sports data timeout for %s — using partial results", title[:50])
        results = {}

    lines = [f"=== SPORTS DATA ({display_name}) ==="]

    # Scoreboard
    scoreboard = results.get("scoreboard")
    if isinstance(scoreboard, list) and scoreboard:
        # Filter to relevant teams if possible
        relevant = [g for g in scoreboard
                    if any(t.upper() in (g["home_abbr"] + g["away_abbr"]).upper() for t in teams)
                    or any(t.lower() in (g["home_team"] + g["away_team"]).lower() for t in team_names)]
        shown = relevant or scoreboard[:5]
        lines.append("Recent/Live Scores:")
        for g in shown:
            flag = " [LIVE]" if g["in_progress"] else (" [Final]" if g["completed"] else "")
            rec  = f" ({g['away_record']} vs {g['home_record']})" if g.get("away_record") else ""
            lines.append(
                f"  {g['away_team']} {g['away_score']} @ {g['home_team']} {g['home_score']}"
                f"{flag}{rec}"
            )

    # TheSportsDB last results
    tsdb_last = results.get("tsdb_last")
    if isinstance(tsdb_last, list) and tsdb_last:
        lines.append(f"Last Results ({team_names[0] if team_names else 'team'}):")
        for g in tsdb_last:
            lines.append(
                f"  {g['date']}: {g['away_team']} {g['away_score']} @ {g['home_team']} {g['home_score']}"
                f"  [{g.get('league','')}]"
            )

    # TheSportsDB next event
    tsdb_next = results.get("tsdb_next")
    if isinstance(tsdb_next, dict) and tsdb_next:
        lines.append(
            f"Next Match: {tsdb_next['away_team']} @ {tsdb_next['home_team']}"
            f"  {tsdb_next['date']} {tsdb_next.get('time','')}  [{tsdb_next.get('venue','')}]"
        )

    # League table
    tsdb_table = results.get("tsdb_table")
    if isinstance(tsdb_table, list) and tsdb_table:
        # Filter to relevant teams
        rel_table = [t for t in tsdb_table
                     if any(n.lower() in t["team"].lower() for n in team_names)] if team_names else []
        shown_table = rel_table or tsdb_table[:8]
        lines.append("League Table (top positions):")
        for t in shown_table:
            lines.append(
                f"  #{t['pos']} {t['team']}: P{t['played']} W{t['won']} D{t.get('drawn','')} L{t['lost']} Pts{t['points']}"
                f"  GD:{t.get('goal_diff','')}"
            )

    # Standings from ESPN
    standings = results.get("standings")
    if isinstance(standings, list) and standings and not tsdb_table:
        rel_s = [s for s in standings if s["team"] in teams] if teams else []
        shown_s = rel_s or standings[:6]
        lines.append("Standings:")
        for s in shown_s:
            stats_str = "  ".join(f"{k}:{v}" for k, v in list(s["stats"].items())[:4])
            lines.append(f"  {s['team']}: {stats_str}")

    # NBA stats
    nba_stats = results.get("nba_stats")
    if isinstance(nba_stats, str) and nba_stats:
        lines.append(nba_stats)

    # MLB today
    mlb_today = results.get("mlb_today")
    if isinstance(mlb_today, list) and mlb_today:
        lines.append("MLB Today's Games:")
        for g in mlb_today:
            lines.append(
                f"  {g['away_team']} @ {g['home_team']}  [{g['status']}]"
                f"  Score: {g['away_score']}-{g['home_score']}"
            )

    # Injuries
    injuries = results.get("injuries")
    if isinstance(injuries, list) and injuries:
        lines.append("Injury Report:")
        for h in injuries[:4]:
            lines.append(f"  • {h}")

    # ESPN news
    espn_news = results.get("espn_news")
    if isinstance(espn_news, list) and espn_news:
        lines.append("ESPN Headlines:")
        for h in espn_news[:5]:
            lines.append(f"  • {h}")

    if len(lines) == 1:
        return ""

    lines.append("=== END SPORTS DATA ===")
    return "\n".join(lines)
