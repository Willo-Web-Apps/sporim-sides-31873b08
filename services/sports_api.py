"""
services/sports_api.py — HTTP clients for football-data.org and balldontlie.io.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from config import FOOTBALL_DATA_API_KEY, LEAGUE_CODES

logger = logging.getLogger(__name__)

FOOTBALL_BASE = "https://api.football-data.org/v4"
BALLDONTLIE_BASE = "https://www.balldontlie.io/api/v1"


# ── Football (football-data.org) ──────────────────────────────────────────────

class FootballDataClient:
    """Async client for football-data.org free tier."""

    def __init__(self) -> None:
        self._headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY} if FOOTBALL_DATA_API_KEY else {}

    async def get_matches(self, league_code: str, days_ahead: int = 7) -> list[dict]:
        """Return upcoming matches for a league as normalised dicts."""
        now = datetime.now(tz=timezone.utc)
        date_from = now.strftime("%Y-%m-%d")
        date_to = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        url = f"{FOOTBALL_BASE}/competitions/{league_code}/matches"
        params = {"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED,TIMED"}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=self._headers, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("FootballData API error for %s: %s", league_code, exc)
            return []

        matches = []
        for m in data.get("matches", []):
            matches.append({
                "external_id": str(m["id"]),
                "league": league_code,
                "home_team": m["homeTeam"]["name"],
                "away_team": m["awayTeam"]["name"],
                "kickoff_time": m["utcDate"],  # ISO string
                "status": "scheduled",
                "home_score": None,
                "away_score": None,
                "winner": "tbd",
            })
        return matches

    async def get_match_result(self, external_id: str, league_code: str) -> dict | None:
        """Fetch the final result of a finished match."""
        url = f"{FOOTBALL_BASE}/matches/{external_id}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=self._headers)
                resp.raise_for_status()
                m = resp.json()
        except Exception as exc:
            logger.warning("FootballData result fetch error (%s): %s", external_id, exc)
            return None

        score = m.get("score", {})
        full = score.get("fullTime", {})
        home_score = full.get("home")
        away_score = full.get("away")
        winner_raw = score.get("winner", "IN_PLAY")

        winner_map = {"HOME_TEAM": "home", "AWAY_TEAM": "away", "DRAW": "draw"}
        winner = winner_map.get(winner_raw, "tbd")

        return {
            "external_id": external_id,
            "status": "finished" if winner != "tbd" else "live",
            "home_score": home_score,
            "away_score": away_score,
            "winner": winner,
        }


# ── Basketball (balldontlie.io) ───────────────────────────────────────────────

class BallDontLieClient:
    """Async client for balldontlie.io NBA API (v1, no key required)."""

    async def get_games(self, days_ahead: int = 7) -> list[dict]:
        """Return upcoming NBA games as normalised dicts."""
        today = datetime.now(tz=timezone.utc)
        dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_ahead + 1)]

        url = f"{BALLDONTLIE_BASE}/games"
        params = {"per_page": 30}
        for d in dates:
            params.setdefault("dates[]", []).append(d) if isinstance(params.get("dates[]"), list) else None

        # balldontlie accepts repeated query params — build manually
        query = "&".join(f"dates[]={d}" for d in dates) + "&per_page=30"
        full_url = f"{url}?{query}"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(full_url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("BallDontLie API error: %s", exc)
            return []

        games = []
        for g in data.get("data", []):
            status = g.get("status", "")
            if status in ("Final",):
                continue  # skip finished games
            home = g["home_team"]["full_name"]
            away = g["visitor_team"]["full_name"]
            date_str = g.get("date", "")
            games.append({
                "external_id": f"nba_{g['id']}",
                "league": "NBA",
                "home_team": home,
                "away_team": away,
                "kickoff_time": date_str,
                "status": "scheduled",
                "home_score": None,
                "away_score": None,
                "winner": "tbd",
            })
        return games

    async def get_game_result(self, game_id: str) -> dict | None:
        """Fetch the result of a finished NBA game."""
        numeric_id = game_id.replace("nba_", "")
        url = f"{BALLDONTLIE_BASE}/games/{numeric_id}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                g = resp.json()
        except Exception as exc:
            logger.warning("BallDontLie result error (%s): %s", game_id, exc)
            return None

        status = g.get("status", "")
        if status != "Final":
            return None

        home_score = g.get("home_team_score", 0)
        away_score = g.get("visitor_team_score", 0)
        if home_score > away_score:
            winner = "home"
        elif away_score > home_score:
            winner = "away"
        else:
            winner = "draw"

        return {
            "external_id": f"nba_{g['id']}",
            "status": "finished",
            "home_score": home_score,
            "away_score": away_score,
            "winner": winner,
        }


# ── DB upsert helper ──────────────────────────────────────────────────────────

async def upsert_matches_to_db(session, matches_data: list[dict]) -> int:
    """Insert or update Match records from API data. Returns count upserted."""
    from sqlalchemy import select
    from models import Match
    from datetime import datetime

    count = 0
    for m in matches_data:
        result = await session.execute(
            select(Match).where(Match.external_id == m["external_id"])
        )
        existing = result.scalar_one_or_none()

        # Parse kickoff time
        kt = m["kickoff_time"]
        if isinstance(kt, str):
            try:
                kt = datetime.fromisoformat(kt.replace("Z", "+00:00"))
            except ValueError:
                kt = datetime.utcnow()

        if existing:
            existing.status = m.get("status", existing.status)
            existing.home_score = m.get("home_score")
            existing.away_score = m.get("away_score")
            existing.winner = m.get("winner", "tbd")
        else:
            session.add(Match(
                external_id=m["external_id"],
                league=m["league"],
                home_team=m["home_team"],
                away_team=m["away_team"],
                kickoff_time=kt,
                status=m.get("status", "scheduled"),
                winner="tbd",
            ))
            count += 1

    await session.commit()
    return count
