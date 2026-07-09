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

    # ── 7. Team form statistics ───────────────────────────────────────────────

    async def get_team_form_stats(self, team_id: int) -> Dict:
        """
        Compute advanced form statistics from a team's last 10 events.

        Fetches GET /team/{team_id}/events/last/0 and parses each event to
        produce:
          - over_2_5_goals_pct: % of games with 3+ total goals
          - first_to_score_pct: % of games where this team scored first
          - first_half_winner_pct: % of games this team won the first half
          - clean_sheet_pct: % of games with no goals conceded
          - avg_goals_scored: average goals scored per game
          - avg_goals_conceded: average goals conceded per game
          - current_win_streak: consecutive wins ending at the most recent game
          - current_unbeaten_streak: consecutive non-losses ending at the most recent game

        Returns empty dict on any error.
        """
        try:
            data = await self._get(f"/team/{team_id}/events/last/0")
            if not data:
                return {}

            events = (data.get("events") or [])[-10:]
            if not events:
                return {}

            over_2_5 = 0
            first_to_score = 0
            first_half_wins = 0
            clean_sheets = 0
            goals_scored_total = 0
            goals_conceded_total = 0
            n = len(events)

            # Streaks — process from most-recent (last element) backwards
            current_win_streak = 0
            current_unbeaten_streak = 0

            results_newest_first: List[str] = []  # "W", "D", "L"

            for ev in events:
                home = ev.get("homeTeam") or {}
                away = ev.get("awayTeam") or {}
                h_score_obj = ev.get("homeScore") or {}
                a_score_obj = ev.get("awayScore") or {}
                h_score = h_score_obj.get("current") or 0
                a_score = a_score_obj.get("current") or 0

                is_home = home.get("id") == team_id
                our_score = h_score if is_home else a_score
                their_score = a_score if is_home else h_score

                total_goals = h_score + a_score
                if total_goals >= 3:
                    over_2_5 += 1

                if their_score == 0:
                    clean_sheets += 1

                goals_scored_total += our_score
                goals_conceded_total += their_score

                # First half winner (period scores)
                h1_h = (h_score_obj.get("period1") or 0)
                h1_a = (a_score_obj.get("period1") or 0)
                our_h1 = h1_h if is_home else h1_a
                their_h1 = h1_a if is_home else h1_h
                if our_h1 > their_h1:
                    first_half_wins += 1

                # First to score — incident data would be ideal but we use
                # period1 score as a proxy: if period1 > 0 for this team and
                # period1 == 0 for opponent we scored first; otherwise skip
                if our_h1 > 0 and their_h1 == 0:
                    first_to_score += 1
                elif our_h1 == 0 and their_h1 > 0:
                    pass  # opponent scored first; no increment
                # both scored or neither — ambiguous; skip

                winner = ev.get("winnerCode")
                if winner == 3:
                    result = "D"
                elif (winner == 1 and is_home) or (winner == 2 and not is_home):
                    result = "W"
                else:
                    result = "L"
                results_newest_first.append(result)

            # results_newest_first[-1] is the most-recent game because events
            # comes sorted oldest-first from the API
            results_newest_first.reverse()  # now index 0 = most-recent

            for r in results_newest_first:
                if r == "W":
                    current_win_streak += 1
                    current_unbeaten_streak += 1
                elif r == "D":
                    current_win_streak = 0  # streak broken for wins
                    current_unbeaten_streak += 1
                else:
                    break  # loss ends both streaks

            # first_to_score denominator: only games where period1 was decisive
            fts_denom = sum(
                1 for ev in events
                if (
                    (ev.get("homeScore") or {}).get("period1") or 0
                ) != (
                    (ev.get("awayScore") or {}).get("period1") or 0
                )
            )

            def _pct(num: int, denom: int) -> float:
                return round(100.0 * num / denom, 1) if denom else 0.0

            return {
                "games_analyzed": n,
                "over_2_5_goals_pct": _pct(over_2_5, n),
                "first_to_score_pct": _pct(first_to_score, fts_denom) if fts_denom else None,
                "first_half_winner_pct": _pct(first_half_wins, n),
                "clean_sheet_pct": _pct(clean_sheets, n),
                "avg_goals_scored": round(goals_scored_total / n, 2),
                "avg_goals_conceded": round(goals_conceded_total / n, 2),
                "current_win_streak": current_win_streak,
                "current_unbeaten_streak": current_unbeaten_streak,
            }
        except Exception as exc:
            logger.debug("SofaScore get_team_form_stats error: %s", exc)
            return {}

    # ── 8. H2H aggregated stats ───────────────────────────────────────────────

    async def get_h2h_stats(self, event_id: int) -> Dict:
        """
        Fetch and aggregate head-to-head statistics for an event's two teams.

        Calls GET /event/{event_id}/h2h and parses teamDuel stats (win counts,
        draws). Also scans the last-10 h2h events to compute:
          - home_wins: wins for the home team of the *current* event
          - away_wins: wins for the away team of the *current* event
          - draws
          - avg_goals_per_game
          - both_teams_scored_pct: % of h2h games where both teams scored

        Returns empty dict on any error.
        """
        try:
            data = await self._get(f"/event/{event_id}/h2h")
            if not data:
                return {}

            # teamDuel block gives aggregate win/draw counts from SofaScore
            team_duel = data.get("teamDuel") or {}
            home_wins_td = team_duel.get("homeWins") or 0
            away_wins_td = team_duel.get("awayWins") or 0
            draws_td = team_duel.get("draws") or 0

            # Also inspect the events list for finer stats
            events = (data.get("events") or [])[:10]
            total_goals = 0
            bts_count = 0  # both teams scored

            for ev in events:
                h = (ev.get("homeScore") or {}).get("current") or 0
                a = (ev.get("awayScore") or {}).get("current") or 0
                total_goals += h + a
                if h > 0 and a > 0:
                    bts_count += 1

            n = len(events)
            avg_goals = round(total_goals / n, 2) if n else 0.0

            def _pct(num: int, denom: int) -> float:
                return round(100.0 * num / denom, 1) if denom else 0.0

            return {
                "home_wins": home_wins_td,
                "away_wins": away_wins_td,
                "draws": draws_td,
                "avg_goals_per_game": avg_goals,
                "both_teams_scored_pct": _pct(bts_count, n),
                "games_analyzed": n,
            }
        except Exception as exc:
            logger.debug("SofaScore get_h2h_stats error: %s", exc)
            return {}

    # ── 9. Goal distribution ─────────────────────────────────────────────────

    async def get_goal_distribution(self, team_id: int) -> Dict:
        """
        Compute when a team typically scores across their last 10 events.

        Fetches incident data (GET /event/{event_id}/incidents) for each of
        the team's last 10 events and buckets goals by match minute:
          - early (0-30 min)
          - mid (31-60 min)
          - late (61-90 min)

        Returns empty dict on any error.
        """
        try:
            data = await self._get(f"/team/{team_id}/events/last/0")
            if not data:
                return {}

            events = (data.get("events") or [])[-10:]
            if not events:
                return {}

            early = mid = late = 0

            async def _fetch_incidents(ev: Dict) -> None:
                nonlocal early, mid, late
                ev_id = ev.get("id")
                if not ev_id:
                    return
                inc_data = await self._get(f"/event/{ev_id}/incidents")
                if not inc_data:
                    return

                home = ev.get("homeTeam") or {}
                is_home = home.get("id") == team_id

                incidents = inc_data.get("incidents") or []
                for inc in incidents:
                    # Only goal incidents for this team
                    if inc.get("incidentType") not in ("goal", "penalty"):
                        continue
                    # SofaScore: isHome True if the scoring team is home
                    inc_is_home = inc.get("isHome", None)
                    if inc_is_home is None:
                        # Fallback: check team field
                        team = (inc.get("team") or {})
                        inc_is_home = team.get("id") == home.get("id")
                    if inc_is_home != is_home:
                        continue  # not our team's goal

                    minute = inc.get("time") or 0
                    if minute <= 30:
                        early += 1
                    elif minute <= 60:
                        mid += 1
                    else:
                        late += 1

            await asyncio.gather(
                *[_fetch_incidents(ev) for ev in events],
                return_exceptions=True,
            )

            total = early + mid + late

            def _pct(num: int) -> float:
                return round(100.0 * num / total, 1) if total else 0.0

            return {
                "early_goals_0_30": early,
                "mid_goals_31_60": mid,
                "late_goals_61_90": late,
                "total_goals": total,
                "early_pct": _pct(early),
                "mid_pct": _pct(mid),
                "late_pct": _pct(late),
            }
        except Exception as exc:
            logger.debug("SofaScore get_goal_distribution error: %s", exc)
            return {}

    # ── 10. Master context builder ────────────────────────────────────────────

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

            home_id = event.get("homeTeamId")
            away_id = event.get("awayTeamId")

            async def _empty_dict() -> Dict:
                return {}

            # Fetch everything in parallel
            gather_results = await asyncio.gather(
                self.get_event_stats(event_id),
                self.get_h2h(event_id),
                self.get_h2h_stats(event_id),
                self.get_team_form_stats(home_id) if home_id else _empty_dict(),
                self.get_team_form_stats(away_id) if away_id else _empty_dict(),
                return_exceptions=True,
            )
            stats = gather_results[0] if not isinstance(gather_results[0], Exception) else {}
            h2h = gather_results[1] if not isinstance(gather_results[1], Exception) else []
            h2h_stats = gather_results[2] if not isinstance(gather_results[2], Exception) else {}
            home_form = gather_results[3] if not isinstance(gather_results[3], Exception) else {}
            away_form = gather_results[4] if not isinstance(gather_results[4], Exception) else {}

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

            # H2H block — qualitative record line using h2h_stats (teamDuel aggregates)
            # plus recent score breakdown from h2h event list
            if h2h_stats:
                hw = h2h_stats.get("home_wins", 0)
                aw = h2h_stats.get("away_wins", 0)
                dr = h2h_stats.get("draws", 0)
                n_h2h = hw + aw + dr
                avg_g = h2h_stats.get("avg_goals_per_game", 0)
                bts = h2h_stats.get("both_teams_scored_pct", 0)
                lines.append(
                    f"H2H record (last {n_h2h}): {home_name} {hw}-{dr}-{aw} vs {away_name}"
                    f" (W-D-L) | avg {avg_g} goals/game | both scored {bts}%"
                )
            elif h2h:
                home_wins = sum(1 for m in h2h if m["winner"] == "home")
                away_wins = sum(1 for m in h2h if m["winner"] == "away")
                draws = sum(1 for m in h2h if m["winner"] == "draw")
                n = len(h2h)
                lines.append(
                    f"H2H (last {n}): {home_name} won {home_wins},"
                    f" {away_name} won {away_wins}, Draw {draws}"
                )

            if h2h:
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
                    lines.append("Recent H2H scores: " + ", ".join(score_strs))

            # Team form stats
            def _fmt_form(name: str, form: Dict) -> Optional[str]:
                if not form:
                    return None
                parts = []
                o25 = form.get("over_2_5_goals_pct")
                fts = form.get("first_to_score_pct")
                h1w = form.get("first_half_winner_pct")
                cs = form.get("clean_sheet_pct")
                avg_s = form.get("avg_goals_scored")
                avg_c = form.get("avg_goals_conceded")
                n = form.get("games_analyzed", 10)
                if o25 is not None:
                    parts.append(f"over 2.5G {o25}%")
                if fts is not None:
                    parts.append(f"scored 1st {fts}%")
                if h1w is not None:
                    parts.append(f"1H wins {h1w}%")
                if cs is not None:
                    parts.append(f"clean sheets {cs}%")
                if avg_s is not None and avg_c is not None:
                    parts.append(f"avg {avg_s} scored / {avg_c} conceded")
                return f"{name} (last {n}): " + ", ".join(parts) if parts else None

            home_form_line = _fmt_form(home_name, home_form)
            away_form_line = _fmt_form(away_name, away_form)
            if home_form_line:
                lines.append(home_form_line)
            if away_form_line:
                lines.append(away_form_line)

            # Streaks
            streak_parts = []
            if home_form:
                ws = home_form.get("current_win_streak", 0)
                ubs = home_form.get("current_unbeaten_streak", 0)
                if ws >= 3:
                    streak_parts.append(f"{home_name}: {ws}W win streak")
                elif ubs >= 3:
                    streak_parts.append(f"{home_name}: {ubs}-game unbeaten run")
            if away_form:
                ws = away_form.get("current_win_streak", 0)
                ubs = away_form.get("current_unbeaten_streak", 0)
                if ws >= 3:
                    streak_parts.append(f"{away_name}: {ws}W win streak")
                elif ubs >= 3:
                    streak_parts.append(f"{away_name}: {ubs}-game unbeaten run")
            if streak_parts:
                lines.append("Streaks: " + " | ".join(streak_parts))

            # Betting insight line — surface the strongest edges
            insights: List[str] = []
            # Over/under 2.5 edge
            h_o25 = home_form.get("over_2_5_goals_pct") if home_form else None
            a_o25 = away_form.get("over_2_5_goals_pct") if away_form else None
            if h2h_stats and h2h_stats.get("avg_goals_per_game", 0) >= 3.0:
                insights.append(
                    f"H2H avg {h2h_stats['avg_goals_per_game']} goals — Over 2.5 edge"
                )
            elif h_o25 is not None and a_o25 is not None:
                combined_o25 = (h_o25 + a_o25) / 2
                if combined_o25 >= 70:
                    insights.append(f"Both teams over 2.5G rate {combined_o25:.0f}% — Over 2.5 edge")
                elif combined_o25 <= 30:
                    insights.append(f"Both teams over 2.5G rate {combined_o25:.0f}% — Under 2.5 edge")
            # Clean sheet / BTTS edge
            if h2h_stats:
                bts_pct = h2h_stats.get("both_teams_scored_pct", 0)
                if bts_pct >= 70:
                    insights.append(f"Both teams scored in {bts_pct}% of H2H — BTTS Yes edge")
                elif bts_pct <= 25:
                    insights.append(f"Both teams scored in only {bts_pct}% of H2H — BTTS No edge")
            # First-to-score domination
            h_fts = home_form.get("first_to_score_pct") if home_form else None
            if h_fts is not None and h_fts >= 80:
                insights.append(f"{home_name} opens scoring in {h_fts}% of games")
            a_fts = away_form.get("first_to_score_pct") if away_form else None
            if a_fts is not None and a_fts >= 80:
                insights.append(f"{away_name} opens scoring in {a_fts}% of games")

            if insights:
                lines.append("Betting edge: " + " | ".join(insights))

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
