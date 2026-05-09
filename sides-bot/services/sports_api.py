"""
services/sports_api.py — SIDES Bot Sports Data Clients
========================================================
Two async API clients:
    FootballDataClient  — football-data.org (Premier League, Champions League, World Cup)
    BallDontLieClient   — balldontlie.io (NBA)

Both clients implement a 10-minute in-memory cache to stay within
free-tier rate limits and reduce latency.

Usage:
    football = FootballDataClient()
    matches = await football.get_upcoming_matches("PL")

    nba = BallDontLieClient()
    games = await nba.get_upcoming_games()
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from config import (
    API_CACHE_TTL_SECONDS,
    BALLDONTLIE_BASE_URL,
    FOOTBALL_DATA_API_KEY,
    FOOTBALL_DATA_BASE_URL,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class _TTLCache:
    """
    Simple in-memory TTL cache for API responses.
    Thread-safe enough for single-process async usage.
    """

    def __init__(self, ttl_seconds: int = API_CACHE_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> Optional[object]:
        if key in self._store:
            stored_at, value = self._store[key]
            if time.monotonic() - stored_at < self._ttl:
                return value
            del self._store[key]
        return None

    def set(self, key: str, value: object) -> None:
        self._store[key] = (time.monotonic(), value)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


# ---------------------------------------------------------------------------
# Football-Data.org Client
# ---------------------------------------------------------------------------

class FootballDataClient:
    """
    Async client for football-data.org v4 API.

    Free tier:
        - 10 requests/minute
        - Premier League (PL), Champions League (CL), World Cup (WC)
        - Historical and upcoming match data

    Rate limit strategy: 10-minute cache on all read endpoints.
    """

    # Map our internal league codes to football-data.org competition codes
    COMPETITION_CODES: dict[str, str] = {
        "PL": "PL",   # Premier League
        "CL": "CL",   # UEFA Champions League
        "WC": "WC",   # FIFA World Cup
    }

    def __init__(self) -> None:
        self._cache = _TTLCache()
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazily initialise the HTTP client with auth headers."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=FOOTBALL_DATA_BASE_URL,
                headers={
                    "X-Auth-Token": FOOTBALL_DATA_API_KEY,
                    "Accept": "application/json",
                },
                timeout=10.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client. Call at shutdown."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_upcoming_matches(self, league_code: str) -> list[dict]:
        """
        Fetch upcoming matches for a league in the next 7 days.

        Args:
            league_code: One of "PL", "CL", "WC"

        Returns:
            List of match dicts with keys:
                external_id, home_team, away_team,
                kickoff_time (datetime UTC), kickoff_display (str),
                league, status
        """
        cache_key = f"upcoming_{league_code}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit: %s", cache_key)
            return cached  # type: ignore

        competition = self.COMPETITION_CODES.get(league_code, league_code)
        now = datetime.now(timezone.utc)
        date_from = now.strftime("%Y-%m-%d")
        date_to = (now + timedelta(days=7)).strftime("%Y-%m-%d")

        try:
            client = self._get_client()
            response = await client.get(
                f"/competitions/{competition}/matches",
                params={
                    "dateFrom": date_from,
                    "dateTo": date_to,
                    "status": "SCHEDULED,TIMED,IN_PLAY",
                },
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "Football-data API error %s for league %s: %s",
                e.response.status_code,
                league_code,
                e.response.text[:200],
            )
            return []
        except httpx.RequestError as e:
            logger.error("Football-data network error for league %s: %s", league_code, e)
            return []

        matches = []
        for raw in data.get("matches", []):
            try:
                kickoff = datetime.fromisoformat(
                    raw["utcDate"].replace("Z", "+00:00")
                )
                match = {
                    "external_id": str(raw["id"]),
                    "home_team": raw["homeTeam"]["shortName"] or raw["homeTeam"]["name"],
                    "away_team": raw["awayTeam"]["shortName"] or raw["awayTeam"]["name"],
                    "kickoff_time": kickoff,
                    "kickoff_display": kickoff.strftime("%a %H:%M"),
                    "league": league_code,
                    "status": raw.get("status", "SCHEDULED").lower(),
                }
                matches.append(match)
            except (KeyError, ValueError) as e:
                logger.warning("Failed to parse match from football-data: %s", e)
                continue

        self._cache.set(cache_key, matches)
        logger.info("Fetched %d upcoming matches for %s", len(matches), league_code)
        return matches

    async def get_match_result(self, external_id: str) -> Optional[dict]:
        """
        Fetch the result of a specific match by its football-data.org ID.

        Args:
            external_id: The match ID from football-data.org

        Returns:
            Dict with keys: status, home_score, away_score, winner
            or None if the match is not finished / on error
        """
        cache_key = f"result_{external_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore

        try:
            client = self._get_client()
            response = await client.get(f"/matches/{external_id}")
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "Football-data API error fetching result %s: %s",
                external_id,
                e.response.status_code,
            )
            return None
        except httpx.RequestError as e:
            logger.error("Network error fetching result %s: %s", external_id, e)
            return None

        status = data.get("status", "")
        if status not in ("FINISHED", "AWARDED"):
            return None  # Match not done yet

        score = data.get("score", {})
        full_time = score.get("fullTime", {})
        home_score = full_time.get("home")
        away_score = full_time.get("away")

        # Determine winner
        winner = "tbd"
        if home_score is not None and away_score is not None:
            if home_score > away_score:
                winner = "home"
            elif away_score > home_score:
                winner = "away"
            else:
                winner = "draw"

        result = {
            "status": "finished",
            "home_score": home_score,
            "away_score": away_score,
            "winner": winner,
        }

        # Cache finished results for longer (they won't change)
        self._cache.set(cache_key, result)
        return result


# ---------------------------------------------------------------------------
# Ball Don't Lie Client (NBA)
# ---------------------------------------------------------------------------

class BallDontLieClient:
    """
    Async client for the Ball Don't Lie NBA API v1.

    Free tier:
        - No API key required for basic endpoints
        - Rate limits apply — mitigated by 10-minute cache
        - Covers all NBA regular season and playoff games
    """

    def __init__(self) -> None:
        self._cache = _TTLCache()
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=BALLDONTLIE_BASE_URL,
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_upcoming_games(self) -> list[dict]:
        """
        Fetch NBA games scheduled in the next 7 days.

        Returns:
            List of game dicts with keys:
                external_id, home_team, away_team,
                kickoff_time (datetime UTC), kickoff_display (str),
                league, status
        """
        cache_key = "nba_upcoming"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore

        now = datetime.now(timezone.utc)
        dates = [
            (now + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(8)  # today + 7 days
        ]

        # Ball Don't Lie accepts multiple dates as repeated params
        params: list[tuple[str, str]] = [("dates[]", d) for d in dates]

        try:
            client = self._get_client()
            response = await client.get("/games", params=params)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            logger.error("BallDontLie API error: %s", e.response.status_code)
            return []
        except httpx.RequestError as e:
            logger.error("BallDontLie network error: %s", e)
            return []

        games = []
        for raw in data.get("data", []):
            try:
                # Ball Don't Lie returns "2025-05-09T00:00:00.000Z" as date
                # Tip-off time is not always available on free tier
                date_str = raw.get("date", "")
                if date_str:
                    kickoff = datetime.fromisoformat(
                        date_str.replace("Z", "+00:00")
                    )
                else:
                    continue

                status_raw = raw.get("status", "").lower()
                if "final" in status_raw or "final" in raw.get("period", ""):
                    status = "finished"
                elif raw.get("period", 0) > 0:
                    status = "live"
                else:
                    status = "scheduled"

                game = {
                    "external_id": f"nba_{raw['id']}",
                    "home_team": raw["home_team"]["full_name"],
                    "away_team": raw["visitor_team"]["full_name"],
                    "kickoff_time": kickoff,
                    "kickoff_display": kickoff.strftime("%a %H:%M"),
                    "league": "NBA",
                    "status": status,
                }
                # Only show scheduled/upcoming games
                if status == "scheduled":
                    games.append(game)

            except (KeyError, ValueError) as e:
                logger.warning("Failed to parse NBA game: %s", e)
                continue

        self._cache.set(cache_key, games)
        logger.info("Fetched %d upcoming NBA games", len(games))
        return games

    async def get_game_result(self, game_id: str) -> Optional[dict]:
        """
        Fetch the result of a specific NBA game.

        Args:
            game_id: External ID in format "nba_<integer>"

        Returns:
            Dict with status, home_score, away_score, winner — or None
        """
        # Strip the "nba_" prefix to get the raw ID
        raw_id = game_id.replace("nba_", "")
        cache_key = f"nba_result_{raw_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore

        try:
            client = self._get_client()
            response = await client.get(f"/games/{raw_id}")
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            logger.error("BallDontLie result error for %s: %s", game_id, e.response.status_code)
            return None
        except httpx.RequestError as e:
            logger.error("BallDontLie network error for %s: %s", game_id, e)
            return None

        game = data.get("data", {})
        status_str = game.get("status", "").lower()

        if "final" not in status_str:
            return None  # Not finished

        home_score = game.get("home_team_score")
        away_score = game.get("visitor_team_score")

        winner = "tbd"
        if home_score is not None and away_score is not None:
            if home_score > away_score:
                winner = "home"
            elif away_score > home_score:
                winner = "away"
            # NBA doesn't have draws

        result = {
            "status": "finished",
            "home_score": home_score,
            "away_score": away_score,
            "winner": winner,
        }
        self._cache.set(cache_key, result)
        return result


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

# Module-level singleton clients (reuse HTTP connections across requests)
_football_client: Optional[FootballDataClient] = None
_nba_client: Optional[BallDontLieClient] = None


def get_football_client() -> FootballDataClient:
    """Return the shared FootballDataClient instance."""
    global _football_client
    if _football_client is None:
        _football_client = FootballDataClient()
    return _football_client


def get_nba_client() -> BallDontLieClient:
    """Return the shared BallDontLieClient instance."""
    global _nba_client
    if _nba_client is None:
        _nba_client = BallDontLieClient()
    return _nba_client


async def get_upcoming_matches(league_code: str) -> list[dict]:
    """
    Unified entry point for getting upcoming matches by league code.

    Args:
        league_code: "PL", "CL", "WC", or "NBA"

    Returns:
        List of match dicts ready for display
    """
    if league_code == "NBA":
        return await get_nba_client().get_upcoming_games()
    else:
        return await get_football_client().get_upcoming_matches(league_code)


async def get_match_result(external_id: str, league: str) -> Optional[dict]:
    """
    Unified entry point for fetching a match result.

    Args:
        external_id: Match ID (with "nba_" prefix for NBA)
        league:      League code to route to correct client
    """
    if league == "NBA":
        return await get_nba_client().get_game_result(external_id)
    else:
        return await get_football_client().get_match_result(external_id)
