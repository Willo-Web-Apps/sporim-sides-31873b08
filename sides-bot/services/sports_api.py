"""
services/sports_api.py — SideUp Bot Sports Data Clients
=========================================================
Two async HTTP clients for fetching match data and results:

    FootballDataClient  — football-data.org v4 API
                          Supports: Premier League (PL), Champions League (CL),
                                    La Liga (LL), Serie A (SA)

    BallDontLieClient   — api.balldontlie.io v1 (NBA)

Both clients:
    - Use an in-memory TTL cache to respect free-tier rate limits
    - Return normalised dicts with consistent keys across sports
    - Support both upcoming match listing and single-match result fetching

Public API:
    get_upcoming_matches(league_code) → list[dict]
    get_match_result(external_id, league) → dict | None
    upsert_matches_to_db(db_session, matches) → None
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    API_CACHE_TTL_SECONDS,
    BALLDONTLIE_BASE_URL,
    FOOTBALL_DATA_API_KEY,
    FOOTBALL_DATA_BASE_URL,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory TTL cache
# ---------------------------------------------------------------------------


class _TTLCache:
    """
    Simple thread-safe-enough in-memory TTL cache for single-process async apps.

    Entries are invalidated on read if older than ttl_seconds.

    Args:
        ttl_seconds: Cache entry lifetime in seconds.
    """

    def __init__(self, ttl_seconds: int = API_CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> Optional[object]:
        """Return cached value if still fresh, else None."""
        entry = self._store.get(key)
        if entry is not None:
            stored_at, value = entry
            if time.monotonic() - stored_at < self._ttl:
                return value
            del self._store[key]
        return None

    def set(self, key: str, value: object) -> None:
        """Store a value with the current timestamp."""
        self._store[key] = (time.monotonic(), value)

    def invalidate(self, key: str) -> None:
        """Remove a specific key from the cache."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Flush all cached entries."""
        self._store.clear()


# ---------------------------------------------------------------------------
# Football-Data.org Client
# ---------------------------------------------------------------------------


class FootballDataClient:
    """
    Async client for the football-data.org v4 REST API.

    Free tier constraints:
        - 10 requests per minute
        - Access to: PL, CL, WC (and others on paid tiers)
        - Rate-limit mitigation: 10-minute in-memory cache

    Competition code mapping:
        PL  → Premier League
        CL  → UEFA Champions League
        WC  → FIFA World Cup
        PD  → La Liga (Primera Division)
        SA  → Serie A

    Docs: https://docs.football-data.org/general/v4/index.html
    """

    # Internal code → football-data.org competition code
    COMPETITION_MAP: dict[str, str] = {
        "PL": "PL",
        "CL": "CL",
        "WC": "WC",
        "LL": "PD",   # La Liga → Primera Division
        "SA": "SA",
    }

    def __init__(self) -> None:
        self._cache = _TTLCache()
        self._client: Optional[httpx.AsyncClient] = None

    def _http(self) -> httpx.AsyncClient:
        """Lazily initialise the HTTP client with auth header."""
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
        """Close the underlying HTTP client. Call during application shutdown."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_matches(
        self,
        league_code: str,
        days_ahead: int = 7,
    ) -> list[dict]:
        """
        Fetch upcoming scheduled matches for a league in the next N days.

        Args:
            league_code: Internal league code ("PL", "CL", "WC", "LL", "SA").
            days_ahead:  How many days ahead to fetch. Default 7.

        Returns:
            List of normalised match dicts with keys:
                external_id    (str) — unique match ID from API
                home_team      (str)
                away_team      (str)
                kickoff_time   (datetime, UTC-aware)
                kickoff_display (str) — short display e.g. "Sat 18:30"
                league         (str) — internal league code
                status         (str) — "scheduled" | "live" | "finished" etc.
        """
        cache_key = f"football_upcoming_{league_code}_{days_ahead}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit: %s", cache_key)
            return cached  # type: ignore[return-value]

        competition = self.COMPETITION_MAP.get(league_code, league_code)
        now = datetime.now(timezone.utc)
        date_from = now.strftime("%Y-%m-%d")
        date_to = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        try:
            resp = await self._http().get(
                f"/competitions/{competition}/matches",
                params={
                    "dateFrom": date_from,
                    "dateTo": date_to,
                    "status": "SCHEDULED,TIMED",
                },
            )
            resp.raise_for_status()
            raw_data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "football-data.org HTTP %s for %s: %s",
                exc.response.status_code,
                league_code,
                exc.response.text[:200],
            )
            return []
        except httpx.RequestError as exc:
            logger.error("football-data.org network error for %s: %s", league_code, exc)
            return []

        matches: list[dict] = []
        for raw in raw_data.get("matches", []):
            try:
                kickoff = datetime.fromisoformat(
                    raw["utcDate"].replace("Z", "+00:00")
                )
                home_name = (
                    raw["homeTeam"].get("shortName")
                    or raw["homeTeam"].get("name", "TBD")
                )
                away_name = (
                    raw["awayTeam"].get("shortName")
                    or raw["awayTeam"].get("name", "TBD")
                )
                status_raw = raw.get("status", "SCHEDULED").upper()
                status = {
                    "SCHEDULED": "scheduled",
                    "TIMED": "scheduled",
                    "IN_PLAY": "live",
                    "PAUSED": "live",
                    "FINISHED": "finished",
                    "POSTPONED": "postponed",
                    "CANCELLED": "cancelled",
                    "AWARDED": "finished",
                }.get(status_raw, "scheduled")

                matches.append({
                    "external_id": str(raw["id"]),
                    "home_team": home_name,
                    "away_team": away_name,
                    "kickoff_time": kickoff,
                    "kickoff_display": kickoff.strftime("%-d %b %H:%M"),
                    "league": league_code,
                    "status": status,
                })
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed match from football-data: %s", exc)
                continue

        self._cache.set(cache_key, matches)
        logger.info("Fetched %d upcoming matches for %s", len(matches), league_code)
        return matches

    async def get_match_result(self, external_id: str) -> Optional[dict]:
        """
        Fetch the result of a specific match by its football-data.org ID.

        Only returns a result dict when the match status is FINISHED or AWARDED.
        Returns None if the match is not yet complete.

        Args:
            external_id: The match's integer ID from football-data.org (as string).

        Returns:
            Dict with keys: status, home_score, away_score, winner
            where winner is "home" | "draw" | "away" | "tbd"
            — or None if match not finished.
        """
        cache_key = f"football_result_{external_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        try:
            resp = await self._http().get(f"/matches/{external_id}")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "football-data.org result error %s for match %s",
                exc.response.status_code,
                external_id,
            )
            return None
        except httpx.RequestError as exc:
            logger.error("Network error fetching match result %s: %s", external_id, exc)
            return None

        status = data.get("status", "").upper()
        if status not in ("FINISHED", "AWARDED"):
            return None

        score = data.get("score", {})
        full_time = score.get("fullTime", {})
        home_score = full_time.get("home")
        away_score = full_time.get("away")

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
        - Covers all NBA regular season and playoff games
        - Rate-limit mitigation: 10-minute in-memory cache

    Docs: https://www.balldontlie.io/
    """

    def __init__(self) -> None:
        self._cache = _TTLCache()
        self._client: Optional[httpx.AsyncClient] = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=BALLDONTLIE_BASE_URL,
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_games(self, days_ahead: int = 7) -> list[dict]:
        """
        Fetch NBA games scheduled in the next N days.

        Args:
            days_ahead: How many days ahead to look. Default 7.

        Returns:
            List of normalised game dicts (same schema as football matches):
                external_id    — "nba_<integer>"
                home_team      — Full team name
                away_team      — Visitor team name
                kickoff_time   — datetime UTC (midnight if tip-off unknown)
                kickoff_display — Short display string
                league         — "NBA"
                status         — "scheduled" | "live" | "finished"
        """
        cache_key = f"nba_upcoming_{days_ahead}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit: %s", cache_key)
            return cached  # type: ignore[return-value]

        now = datetime.now(timezone.utc)
        dates = [
            (now + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days_ahead + 1)
        ]

        # Ball Don't Lie accepts multiple date params
        params: list[tuple[str, str]] = [("dates[]", d) for d in dates]

        try:
            resp = await self._http().get("/games", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("BallDontLie HTTP %s: %s", exc.response.status_code, exc.response.text[:100])
            return []
        except httpx.RequestError as exc:
            logger.error("BallDontLie network error: %s", exc)
            return []

        games: list[dict] = []
        for raw in data.get("data", []):
            try:
                date_str = raw.get("date", "")
                if not date_str:
                    continue

                # Parse the game date (may be midnight UTC)
                if "T" in date_str:
                    kickoff = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                else:
                    kickoff = datetime.strptime(date_str, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )

                # Status detection
                status_raw = str(raw.get("status", "")).lower()
                period = raw.get("period", 0) or 0
                if "final" in status_raw:
                    status = "finished"
                elif period > 0:
                    status = "live"
                else:
                    status = "scheduled"

                # Only include scheduled games in the upcoming list
                if status != "scheduled":
                    continue

                games.append({
                    "external_id": f"nba_{raw['id']}",
                    "home_team": raw["home_team"]["full_name"],
                    "away_team": raw["visitor_team"]["full_name"],
                    "kickoff_time": kickoff,
                    "kickoff_display": kickoff.strftime("%-d %b"),
                    "league": "NBA",
                    "status": status,
                })
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed NBA game: %s", exc)
                continue

        self._cache.set(cache_key, games)
        logger.info("Fetched %d upcoming NBA games", len(games))
        return games

    async def get_game_result(self, game_id: str) -> Optional[dict]:
        """
        Fetch the result of a specific NBA game.

        Args:
            game_id: External ID in the format "nba_<integer>".

        Returns:
            Dict with status, home_score, away_score, winner
            — or None if not finished.
        """
        raw_id = game_id.removeprefix("nba_")
        cache_key = f"nba_result_{raw_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        try:
            resp = await self._http().get(f"/games/{raw_id}")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("BallDontLie result error %s for %s", exc.response.status_code, game_id)
            return None
        except httpx.RequestError as exc:
            logger.error("BallDontLie network error for %s: %s", game_id, exc)
            return None

        game = data.get("data", {})
        status_str = str(game.get("status", "")).lower()

        if "final" not in status_str:
            return None

        home_score = game.get("home_team_score")
        away_score = game.get("visitor_team_score")

        winner = "tbd"
        if home_score is not None and away_score is not None:
            # NBA has no draws
            winner = "home" if home_score > away_score else "away"

        result = {
            "status": "finished",
            "home_score": home_score,
            "away_score": away_score,
            "winner": winner,
        }
        self._cache.set(cache_key, result)
        return result


# ---------------------------------------------------------------------------
# Module-level singleton clients
# ---------------------------------------------------------------------------

_football_client: Optional[FootballDataClient] = None
_nba_client: Optional[BallDontLieClient] = None


def get_football_client() -> FootballDataClient:
    """Return the shared FootballDataClient singleton."""
    global _football_client
    if _football_client is None:
        _football_client = FootballDataClient()
    return _football_client


def get_nba_client() -> BallDontLieClient:
    """Return the shared BallDontLieClient singleton."""
    global _nba_client
    if _nba_client is None:
        _nba_client = BallDontLieClient()
    return _nba_client


# ---------------------------------------------------------------------------
# Unified entry points
# ---------------------------------------------------------------------------


async def get_upcoming_matches(league_code: str) -> list[dict]:
    """
    Unified entry point for fetching upcoming matches by league code.

    Routes to the correct API client based on the league code:
        "NBA"           → BallDontLieClient.get_games()
        "PL","CL",etc.  → FootballDataClient.get_matches()

    Args:
        league_code: "PL" | "CL" | "WC" | "LL" | "SA" | "NBA"

    Returns:
        Normalised list of match dicts (see individual client docstrings).
    """
    if league_code == "NBA":
        return await get_nba_client().get_games()
    return await get_football_client().get_matches(league_code)


async def get_match_result(external_id: str, league: str) -> Optional[dict]:
    """
    Unified entry point for fetching a match result.

    Routes to the correct API client based on the league.

    Args:
        external_id: Match/game ID (use "nba_<id>" format for NBA).
        league:      League code to route to correct client.

    Returns:
        Dict with status, home_score, away_score, winner — or None if not finished.
    """
    if league == "NBA":
        return await get_nba_client().get_game_result(external_id)
    return await get_football_client().get_match_result(external_id)


async def upsert_matches_to_db(
    session: AsyncSession,
    matches_data: list[dict],
) -> int:
    """
    Persist a list of normalised match dicts to the database.

    Uses get_or_create_match from challenge_service to upsert each record,
    ensuring no duplicates are created for already-cached matches.

    Args:
        session:      Active AsyncSession.
        matches_data: List of normalised match dicts from get_upcoming_matches().

    Returns:
        Number of matches upserted (created or updated).
    """
    from services.challenge_service import get_or_create_match

    count = 0
    for match_data in matches_data:
        try:
            await get_or_create_match(session, match_data)
            count += 1
        except Exception as exc:
            logger.warning(
                "Failed to upsert match %s: %s",
                match_data.get("external_id", "?"),
                exc,
            )

    logger.info("Upserted %d/%d matches to DB.", count, len(matches_data))
    return count


async def close_all_clients() -> None:
    """Close all API HTTP clients. Call during application shutdown."""
    if _football_client:
        await _football_client.close()
    if _nba_client:
        await _nba_client.close()
    logger.info("Sports API HTTP clients closed.")
