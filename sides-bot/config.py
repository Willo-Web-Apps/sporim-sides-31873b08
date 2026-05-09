"""
config.py — SIDES Bot Configuration
=====================================
Loads all environment variables from .env and exposes them as typed
Python constants. Import this module anywhere you need configuration.

Usage:
    from config import BOT_TOKEN, ADMIN_USER_IDS, PLATFORM_FEE_PERCENT
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env file (no-op in production where vars are set at system level)
# ---------------------------------------------------------------------------
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path)


# ---------------------------------------------------------------------------
# Helper: require an env var or raise a clear error at startup
# ---------------------------------------------------------------------------
def _require(var_name: str) -> str:
    """Return env var value or raise RuntimeError with a helpful message."""
    value = os.getenv(var_name)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{var_name}' is not set. "
            f"Copy .env.example to .env and fill in your values."
        )
    return value


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

BOT_TOKEN: str = _require("BOT_TOKEN")
"""Telegram bot token from @BotFather. Keep this secret."""

BOT_USERNAME: str = os.getenv("BOT_USERNAME", "sideupbot")
"""Bot username (without @). Used to generate invite links."""

_raw_admin_ids: str = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS: list[int] = (
    [int(uid.strip()) for uid in _raw_admin_ids.split(",") if uid.strip()]
    if _raw_admin_ids
    else []
)
"""
List of Telegram user IDs with admin access.
Set via ADMIN_USER_IDS=123456789,987654321 in .env
"""

# ---------------------------------------------------------------------------
# Sports APIs
# ---------------------------------------------------------------------------

FOOTBALL_DATA_API_KEY: str = os.getenv("FOOTBALL_DATA_API_KEY", "")
"""
API key for football-data.org (free tier).
Register at: https://www.football-data.org/client/register
Required for Premier League, Champions League, and World Cup data.
"""

FOOTBALL_DATA_BASE_URL: str = "https://api.football-data.org/v4"
"""Base URL for the football-data.org v4 API."""

BALLDONTLIE_BASE_URL: str = "https://api.balldontlie.io/v1"
"""Base URL for the Ball Don't Lie NBA API (free tier, no key required)."""

# Supported league codes for football-data.org
LEAGUE_CODES: dict[str, str] = {
    "PL": "Premier League",
    "CL": "Champions League",
    "WC": "World Cup",
}

# Cache TTL for sports API responses (seconds)
API_CACHE_TTL_SECONDS: int = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///sides.db")
"""
SQLAlchemy async database URL.
Default: SQLite (development). Switch to postgresql+asyncpg:// for production.
"""

# ---------------------------------------------------------------------------
# Business Logic
# ---------------------------------------------------------------------------

PLATFORM_FEE_PERCENT: float = 0.02
"""
Platform fee as a decimal (2%). Charged on challenge resolution.
Phase 1 (0-1000 users): fee is recorded but NOT deducted — free period.
Change to active collection in Phase 2.
"""

MIN_CHALLENGE_AMOUNT: int = 1
"""Minimum challenge amount in Telegram Stars (⭐)."""

MAX_CHALLENGE_AMOUNT: int = 10_000
"""Maximum challenge amount in Telegram Stars (⭐)."""

CHALLENGE_EXPIRY_HOURS: int = 48
"""Hours after creation before an unaccepted challenge expires."""

MARKET_PAGE_SIZE: int = 5
"""Number of challenges per page in The Market listing."""

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
"""Runtime environment: 'development' or 'production'."""

IS_PRODUCTION: bool = ENVIRONMENT == "production"

RESULT_CHECK_INTERVAL_MINUTES: int = 15
"""How often the APScheduler job checks for match results (minutes)."""

# ---------------------------------------------------------------------------
# Validation at import time
# ---------------------------------------------------------------------------

if not ADMIN_USER_IDS:
    import warnings
    warnings.warn(
        "ADMIN_USER_IDS is not set. No users will have admin access. "
        "Set ADMIN_USER_IDS=your_telegram_id in .env",
        stacklevel=2,
    )
