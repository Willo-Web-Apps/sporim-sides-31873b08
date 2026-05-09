"""
config.py — SIDES Bot Configuration
Loads all environment variables with validation.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Required environment variable '{key}' is not set. Check your .env file.")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str = _require("BOT_TOKEN")
BOT_USERNAME: str = _optional("BOT_USERNAME", "sideupbot")

# Comma-separated list of Telegram user IDs with admin access
_raw_admins = _optional("ADMIN_USER_IDS", "")
ADMIN_USER_IDS: list[int] = [int(x.strip()) for x in _raw_admins.split(",") if x.strip()]

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL: str = _optional("DATABASE_URL", "sqlite+aiosqlite:///sides.db")

# ── Sports APIs ───────────────────────────────────────────────────────────────
FOOTBALL_DATA_API_KEY: str = _optional("FOOTBALL_DATA_API_KEY", "")

# ── Business Rules ────────────────────────────────────────────────────────────
PLATFORM_FEE_PERCENT: float = 0.02          # 2% of pot — Phase 2 (currently 0% in Phase 1)
PHASE_1_FREE: bool = True                   # Set to False to enable fees
MIN_CHALLENGE_AMOUNT: int = 1               # Minimum Stars per side
MAX_CHALLENGE_AMOUNT: int = 10_000          # Maximum Stars per side
CHALLENGE_EXPIRY_HOURS: int = 72            # Auto-cancel unaccepted challenges after 72h

# ── Environment ───────────────────────────────────────────────────────────────
ENVIRONMENT: str = _optional("ENVIRONMENT", "development")
IS_PRODUCTION: bool = ENVIRONMENT == "production"

# ── League Codes (football-data.org) ──────────────────────────────────────────
LEAGUE_CODES = {
    "premier_league": "PL",
    "champions_league": "CL",
    "world_cup": "WC",
    "serie_a": "SA",
    "la_liga": "PD",
}
