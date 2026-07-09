"""
SofaScore unofficial API client for live sports data.

Provides live scores, match statistics, head-to-head records, team form,
and player stats to enrich AI trading decisions on sports markets.

API base: https://api.sofascore.com/api/v1
No API key required.
"""

import asyncio
import datetime
import logging
import re
import time
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

_ET = ZoneInfo("America/New_York")

logger = logging.getLogger("trading.sofascore")

_BASE_URL = "https://api.sofascore.com/api/v1"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.sofascore.com",
    "Accept": "application/json",
}

_ALL_SPORTS = [
    "football",
    "basketball",
    "tennis",
    "baseball",
    "american-football",
    "ice-hockey",
    "mma",
    "boxing",
    "volleyball",
]

_LIVE_CACHE_TTL = 60  # seconds


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation for fuzzy team matching."""
    return re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()


class SofaScoreClient:
    """
    Async client for the SofaScore unofficial REST API.

    Usage:
        async with SofaScoreClient() as client:
            ctx = await client.build_match_context("France", "Morocco")
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        # Cache: (timestamp, list_of_events)
        self._live_cache: Tuple[float, List[Dict]] = (0.0, [])

    async def __aenter__(self) -> "SofaScoreClient":
        self._client = httpx.AsyncClient(
            headers=_HEADERS,
            timeout=10,
            trust_env=False,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str) -> Optional[Dict]:
        """GET a SofaScore API path; returns parsed JSON or None on any error."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=_HEADERS,
                timeout=10,
                trust_env=False,
                follow_redirects=True,
            )
        url = f"{_BASE_URL}{path}"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 404:
                logger.debug("SofaScore 404: %s", path)
                return None
            if resp.status_code != 200:
                logger.debug("SofaScore %d: %s", resp.status_code, path)
                return None
            return resp.json()
        except httpx.TimeoutException:
            logger.debug("SofaScore timeout: %s", path)
            return None
        except Exception as exc:
            logger.debug("SofaScore error for %s: %s", path, exc)
            return None

    # ── 1. Live events ────────────────────────────────────────────────────────

    async def get_live_events(self, sport: str = None) -> List[Dict]:
        """
        Fetch live events for a specific sport or all supported sports.

        Returns a list of normalized event dicts with id, teams, scores,
        game time, period, status, and tournament name.
        """
        now = time.monotonic()
        cache_age = now - self._live_cache[0]
        if cache_age < _LIVE_CACHE_TTL and self._live_cache[1]:
            cached = self._live_cache[1]
            if sport is None:
                return cached
            return [e for e in cached if e.get("_sport") == sport]

        sports = [sport] if sport else _ALL_SPORTS

        async def _fetch_sport(s: str) -> List[Dict]:
            data = await self._get(f"/sport/{s}/events/live")
            if not data:
                return []
            events = data.get("events") or []
            out = []
            for ev in events:
                try:
                    out.append(self._normalize_event(ev, sport_slug=s))
                except Exception as exc:
                    logger.debug("SofaScore event parse error: %s", exc)
            return out

        results = await asyncio.gather(*[_fetch_sport(s) for s in sports], return_exceptions=True)
        all_events: List[Dict] = []
        for r in results:
            if isinstance(r, list):
                all_events.extend(r)

        self._live_cache = (now, all_events)

        if sport:
            return [e for e in all_events if e.get("_sport") == sport]
        return all_events

    @staticmethod
    def _normalize_event(ev: Dict, sport_slug: str = "") -> Dict:
        """Extract a flat, normalized dict from a raw SofaScore event."""
        home = ev.get("homeTeam") or {}
        away = ev.get("awayTeam") or {}
        score = ev.get("homeScore") or {}
        away_score = ev.get("awayScore") or {}
        status = ev.get("status") or {}
        tournament = ev.get("tournament") or {}

        # Game time: prefer displayTime, fall back to time seconds
        game_time = status.get("displayTime") or ""
        if not game_time:
            elapsed = ev.get("time", {}).get("played") if ev.get("time") else None
            if elapsed is not None:
                minutes, secs = divmod(int(elapsed), 60)
                game_time = f"{minutes}:{secs:02d}"

        period_map = {
            "1ST": "1H", "2ND": "2H",
            "1ST_EXTRA": "ET1", "2ND_EXTRA": "ET2",
            "PENALTIES": "PEN",
            "Q1": "Q1", "Q2": "Q2", "Q3": "Q3", "Q4": "Q4",
            "OT": "OT",
            "1": "P1", "2": "P2", "3": "P3",
            "SET1": "S1", "SET2": "S2", "SET3": "S3",
            "INNING1": "I1",
        }
        raw_period = (status.get("period") or "").upper()
        period = period_map.get(raw_period, raw_period or "LIVE")

        return {
            "id": ev.get("id"),
            "slug": ev.get("slug", ""),
            "homeTeam": home.get("name", ""),
            "awayTeam": away.get("name", ""),
            "homeTeamId": home.get("id"),
            "awayTeamId": away.get("id"),
            "homeScore": score.get("current"),
            "awayScore": away_score.get("current"),
            "gameTime": game_time,
            "period": period,
            "status": status.get("type", "inprogress"),
            "tournament": tournament.get("name", ""),
            "_sport": sport_slug,
        }

    # ── 2. Search event ───────────────────────────────────────────────────────

    async def search_event(self, home_team: str, away_team: str) -> Optional[Dict]:
        """
        Search live events for a match between home_team and away_team.

        Uses fuzzy (normalized) matching on team names. Returns the first
        matching event dict or None.
        """
        try:
            events = await self.get_live_events()
            home_norm = _normalize(home_team)
            away_norm = _normalize(away_team)

            for ev in events:
                ev_home = _normalize(ev.get("homeTeam", ""))
                ev_away = _normalize(ev.get("awayTeam", ""))

                home_match = home_norm in ev_home or ev_home in home_norm
                away_match = away_norm in ev_away or ev_away in away_norm

                # Also try reversed (team listed as away may be given as home)
                home_match_rev = home_norm in ev_away or ev_away in home_norm
                away_match_rev = away_norm in ev_home or ev_home in away_norm

                if (home_match and away_match) or (home_match_rev and away_match_rev):
                    return ev

            return None
        except Exception as exc:
            logger.debug("SofaScore search_event error: %s", exc)
            return None

    # ── 3. Event statistics ───────────────────────────────────────────────────

    async def get_event_stats(self, event_id: int) -> Dict:
        """
        Fetch statistics for a live/finished event.

        Returns a flat dict: possession_home, possession_away, shots_home,
        shots_away, shots_on_target_home, shots_on_target_away, corners_home,
        corners_away, fouls_home, fouls_away, yellow_cards_home, etc.
        """
        try:
            data = await self._get(f"/event/{event_id}/statistics")
            if not data:
                return {}

            stats: Dict = {}
            # SofaScore returns periods; grab first "ALL" or the first group
            groups = data.get("statistics") or []
            period_stats = None
            for group in groups:
                if group.get("period", "").upper() == "ALL":
                    period_stats = group
                    break
            if period_stats is None and groups:
                period_stats = groups[0]
            if period_stats is None:
                return {}

            _key_map = {
                "Ball possession": ("possession_home", "possession_away"),
                "Total shots": ("shots_home", "shots_away"),
                "Shots on target": ("shots_on_target_home", "shots_on_target_away"),
                "Corner kicks": ("corners_home", "corners_away"),
                "Fouls": ("fouls_home", "fouls_away"),
                "Yellow cards": ("yellow_cards_home", "yellow_cards_away"),
                "Red cards": ("red_cards_home", "red_cards_away"),
                "Offsides": ("offsides_home", "offsides_away"),
                "Goalkeeper saves": ("saves_home", "saves_away"),
            }

            for stat_group in (period_stats.get("groups") or []):
                for item in (stat_group.get("statisticsItems") or []):
                    name = item.get("name", "")
                    if name in _key_map:
                        hk, ak = _key_map[name]
                        stats[hk] = item.get("home")
                        stats[ak] = item.get("away")

            return stats
        except Exception as exc:
            logger.debug("SofaScore get_event_stats error: %s", exc)
            return {}

    # ── 4. Head-to-head ───────────────────────────────────────────────────────

    async def get_h2h(self, event_id: int) -> List[Dict]:
        """
        Fetch head-to-head history for two teams.

        Returns up to last 10 matches with: date, homeTeam, awayTeam,
        homeScore, awayScore, winner (home/away/draw).
        """
        try:
            data = await self._get(f"/event/{event_id}/h2h/events")
            if not data:
                return []

            events = data.get("events") or []
            out = []
            for ev in events[:10]:
                home = (ev.get("homeTeam") or {}).get("name", "")
                away = (ev.get("awayTeam") or {}).get("name", "")
                h_score = (ev.get("homeScore") or {}).get("current")
                a_score = (ev.get("awayScore") or {}).get("current")
                winner = ev.get("winnerCode")  # 1=home, 2=away, 3=draw
                winner_str = {1: "home", 2: "away", 3: "draw"}.get(winner, "unknown")

                # Date from startTimestamp
                ts = ev.get("startTimestamp")
                date_str = ""
                if ts:
                    date_str = datetime.datetime.fromtimestamp(ts, tz=_ET).strftime("%Y-%m-%d")

                out.append({
                    "date": date_str,
                    "homeTeam": home,
                    "awayTeam": away,
                    "homeScore": h_score,
                    "awayScore": a_score,
                    "winner": winner_str,
                })
            return out
        except Exception as exc:
            logger.debug("SofaScore get_h2h error: %s", exc)
            return []

    # ── 5. Team form ──────────────────────────────────────────────────────────

    async def get_team_form(self, team_id: int) -> List[Dict]:
        """
        Fetch last 5 results for a team.

        Returns list of dicts with: date, opponent, homeScore, awayScore,
        result (W/L/D from team's perspective), was_home.
        """
        try:
            data = await self._get(f"/team/{team_id}/events/last/0")
            if not data:
                return []

            events = data.get("events") or []
            out = []
            for ev in events[-5:]:
                home = ev.get("homeTeam") or {}
                away = ev.get("awayTeam") or {}
                h_score = (ev.get("homeScore") or {}).get("current")
                a_score = (ev.get("awayScore") or {}).get("current")
                winner = ev.get("winnerCode")

                is_home = home.get("id") == team_id
                opponent = away.get("name", "") if is_home else home.get("name", "")

                if winner == 3:
                    result = "D"
                elif (winner == 1 and is_home) or (winner == 2 and not is_home):
                    result = "W"
                else:
                    result = "L"

                ts = ev.get("startTimestamp")
                date_str = ""
                if ts:
                    date_str = datetime.datetime.fromtimestamp(ts, tz=_ET).strftime("%Y-%m-%d")

                out.append({
                    "date": date_str,
                    "opponent": opponent,
                    "homeScore": h_score,
                    "awayScore": a_score,
                    "result": result,
                    "was_home": is_home,
                })
            return out
        except Exception as exc:
            logger.debug("SofaScore get_team_form error: %s", exc)
            return []

    # ── 6. Player statistics ──────────────────────────────────────────────────

    async def get_player_stats(self, player_id: int, tournament_id: int = None) -> Dict:
        """
        Fetch career statistics for a player.

        Returns averages: goals_per_game, assists_per_game, shots_per_game,
        rating, appearances, and raw season breakdown.
        """
        try:
            data = await self._get(f"/player/{player_id}/statistics/career")
            if not data:
                return {}

            seasons = data.get("seasons") or []
            if not seasons:
                return {}

            # If tournament_id given, filter to that tournament
            if tournament_id:
                filtered = [
                    s for s in seasons
                    if (s.get("tournament") or {}).get("id") == tournament_id
                ]
                if filtered:
                    seasons = filtered

            # Aggregate across selected seasons
            total_goals = total_assists = total_shots = total_rating = 0
            total_appearances = 0
            rating_count = 0

            for season in seasons:
                stats = season.get("statistics") or {}
                apps = stats.get("appearances") or 0
                total_appearances += apps
                total_goals += stats.get("goals") or 0
                total_assists += stats.get("assists") or 0
                total_shots += stats.get("totalShots") or 0
                rating = stats.get("rating")
                if rating:
                    total_rating += float(rating) * apps
                    rating_count += apps

            if total_appearances == 0:
                return {}

            return {
                "appearances": total_appearances,
                "goals_per_game": round(total_goals / total_appearances, 2),
                "assists_per_game": round(total_assists / total_appearances, 2),
                "shots_per_game": round(total_shots / total_appearances, 2),
                "avg_rating": round(total_rating / rating_count, 2) if rating_count else None,
                "total_goals": total_goals,
                "total_assists": total_assists,
            }
        except Exception as exc:
            logger.debug("SofaScore get_player_stats error: %s", exc)
            return {}

    # ── 7. Master context builder ─────────────────────────────────────────────

    async def build_match_context(self, home_team: str, away_team: str) -> str:
        """
        Build a formatted context string for a live match between two teams.

        Searches for the live event, fetches stats and H2H, and formats
        everything into a compact multi-line block for AI prompt injection.

        Returns empty string if the match is not live or any critical step fails.
        """
        try:
            event = await self.search_event(home_team, away_team)
            if not event:
                logger.debug("SofaScore: no live event found for %s vs %s", home_team, away_team)
                return ""

            event_id = event["id"]
            home_name = event.get("homeTeam", home_team)
            away_name = event.get("awayTeam", away_team)
            h_score = event.get("homeScore", "?")
            a_score = event.get("awayScore", "?")
            game_time = event.get("gameTime", "")
            period = event.get("period", "")
            tournament = event.get("tournament", "")

            # Fetch stats and H2H in parallel
            stats, h2h = await asyncio.gather(
                self.get_event_stats(event_id),
                self.get_h2h(event_id),
                return_exceptions=True,
            )
            if isinstance(stats, Exception):
                stats = {}
            if isinstance(h2h, Exception):
                h2h = []

            # Header line
            time_part = f"{game_time} | " if game_time else ""
            period_part = f" | Period: {period}" if period else ""
            header = (
                f"[SOFASCORE LIVE] {home_name} vs {away_name}"
                f"{' — ' + tournament if tournament else ''}"
                f" — {time_part}Score: {h_score}-{a_score}{period_part}"
            )

            lines = [header]

            # Stats block
            if stats:
                poss_h = stats.get("possession_home", "?")
                poss_a = stats.get("possession_away", "?")
                if poss_h != "?" or poss_a != "?":
                    lines.append(
                        f"Possession: {home_name} {poss_h}% {away_name} {poss_a}%"
                    )

                shots_h = stats.get("shots_home", "?")
                shots_a = stats.get("shots_away", "?")
                sot_h = stats.get("shots_on_target_home", "?")
                sot_a = stats.get("shots_on_target_away", "?")
                if shots_h != "?" or shots_a != "?":
                    lines.append(
                        f"Shots: {home_name} {shots_h} {away_name} {shots_a}"
                        f" | On target: {home_name} {sot_h} {away_name} {sot_a}"
                    )

                corners_h = stats.get("corners_home")
                corners_a = stats.get("corners_away")
                fouls_h = stats.get("fouls_home")
                fouls_a = stats.get("fouls_away")
                extras = []
                if corners_h is not None:
                    extras.append(f"Corners: {home_name} {corners_h} {away_name} {corners_a}")
                if fouls_h is not None:
                    extras.append(f"Fouls: {home_name} {fouls_h} {away_name} {fouls_a}")
                if extras:
                    lines.append(" | ".join(extras))

            # H2H block
            if h2h:
                home_wins = sum(1 for m in h2h if m["winner"] == "home")
                away_wins = sum(1 for m in h2h if m["winner"] == "away")
                draws = sum(1 for m in h2h if m["winner"] == "draw")
                n = len(h2h)
                lines.append(
                    f"H2H (last {n}): {home_name} won {home_wins},"
                    f" {away_name} won {away_wins}, Draw {draws}"
                )

                # Recent scores — last 5
                score_strs = []
                for m in h2h[:5]:
                    hs = m.get("homeScore")
                    as_ = m.get("awayScore")
                    date = m.get("date", "")
                    year = date[:4] if date else ""
                    mh = m.get("homeTeam", "")
                    ma = m.get("awayTeam", "")
                    if hs is not None and as_ is not None:
                        score_strs.append(f"{mh} {hs}-{as_} {ma} ({year})")
                if score_strs:
                    lines.append("Recent scores: " + ", ".join(score_strs))

            return "\n".join(lines)

        except Exception as exc:
            logger.debug("SofaScore build_match_context error: %s", exc)
            return ""

    # ── 8. Team extraction from market title ──────────────────────────────────

    @staticmethod
    def extract_teams_from_title(title: str) -> Optional[Tuple[str, str]]:
        """
        Parse team names from a market title containing "vs" or "v" separator.

        Handles patterns like:
          - "France vs Morocco"
          - "Lakers vs. Celtics"
          - "Man City v Arsenal"
          - "Will France beat Morocco?"

        Returns (home_team, away_team) tuple, or None if no match found.
        """
        # Pattern: <team> vs? <team>   (case-insensitive)
        match = re.search(
            r"([A-Za-z0-9 .&'()+-]{2,40?}?)\s+vs?\.?\s+([A-Za-z0-9 .&'()+-]{2,40})",
            title,
            re.IGNORECASE,
        )
        if not match:
            return None

        home = match.group(1).strip()
        away = match.group(2).strip()

        # Strip leading noise words that sometimes appear before the home team
        _noise = re.compile(
            r"^(will|who wins|can|does|is|are|the|when)\s+",
            re.IGNORECASE,
        )
        home = _noise.sub("", home).strip()

        # Strip trailing punctuation from away (e.g. "Morocco?")
        away = re.sub(r"[?!.]+$", "", away).strip()

        # Sanity: both sides should be at least 2 chars
        if len(home) < 2 or len(away) < 2:
            return None

        return (home, away)
