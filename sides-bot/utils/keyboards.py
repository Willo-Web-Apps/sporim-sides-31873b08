"""
utils/keyboards.py — SIDES Bot Inline Keyboard Builders
=========================================================
Centralised factory functions for all InlineKeyboardMarkup objects.
Keep keyboard logic here so handlers stay clean.

All functions return a telegram.InlineKeyboardMarkup ready to pass
as the reply_markup argument to any send/edit message call.
"""

import math
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import MARKET_PAGE_SIZE


# ---------------------------------------------------------------------------
# Main Menu
# ---------------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Primary welcome screen keyboard.
    Shown on /start with no arguments.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏆 Create Challenge", callback_data="create_challenge"),
        ],
        [
            InlineKeyboardButton("📋 The Market", callback_data="open_market"),
            InlineKeyboardButton("ℹ️ How It Works", callback_data="how_it_works"),
        ],
    ])


# ---------------------------------------------------------------------------
# Challenge Creation Flow
# ---------------------------------------------------------------------------

def league_keyboard() -> InlineKeyboardMarkup:
    """
    League selection keyboard for challenge creation.
    Shows available leagues with emoji flags.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League",
                callback_data="league_PL",
            ),
        ],
        [
            InlineKeyboardButton("🏀 NBA", callback_data="league_NBA"),
            InlineKeyboardButton("🌍 Champions League", callback_data="league_CL"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ],
    ])


def matches_keyboard(
    matches: list[dict],
    league: str,
) -> InlineKeyboardMarkup:
    """
    Dynamic match selection keyboard.
    Shows up to 8 upcoming matches as buttons.

    Args:
        matches: List of match dicts with keys:
                 external_id, home_team, away_team, kickoff_time (str)
        league:  League code (for callback data routing)

    Returns:
        InlineKeyboardMarkup with one match per row + Cancel button
    """
    buttons: list[list[InlineKeyboardButton]] = []

    for match in matches[:8]:  # Cap at 8 to avoid Telegram button limits
        label = (
            f"{match['home_team']} vs {match['away_team']} "
            f"· {match['kickoff_display']}"
        )
        # Truncate label if too long (Telegram max ~40 chars per button label)
        if len(label) > 50:
            label = label[:47] + "…"

        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"match_{match['external_id']}",
            )
        ])

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def sides_keyboard(home_team: str, away_team: str) -> InlineKeyboardMarkup:
    """
    Side selection keyboard — Home / Draw / Away.
    Team names are truncated to 20 chars to fit Telegram button limits.

    Args:
        home_team: Name of the home team
        away_team: Name of the away team
    """
    home_short = home_team[:20] if len(home_team) > 20 else home_team
    away_short = away_team[:20] if len(away_team) > 20 else away_team

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"🏠 {home_short} (Home)",
                callback_data="side_home",
            ),
        ],
        [
            InlineKeyboardButton("🤝 Draw", callback_data="side_draw"),
        ],
        [
            InlineKeyboardButton(
                f"✈️ {away_short} (Away)",
                callback_data="side_away",
            ),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ],
    ])


def confirm_challenge_keyboard() -> InlineKeyboardMarkup:
    """Confirm or cancel before creating a challenge."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm & Create", callback_data="confirm_create"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ],
    ])


def challenge_share_keyboard(
    invite_link: str,
    challenge_uuid: str,
) -> InlineKeyboardMarkup:
    """
    Post-creation keyboard shown to the challenge creator.

    Args:
        invite_link:     Full t.me deep link for the challenge
        challenge_uuid:  Used to route the "Post to Market" action
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📤 Share Invite Link",
                url=invite_link,
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 Post to Market",
                callback_data=f"post_market_{challenge_uuid}",
            ),
        ],
    ])


# ---------------------------------------------------------------------------
# The Market
# ---------------------------------------------------------------------------

def market_keyboard(
    challenges: list,  # list[Challenge] — avoid circular import
    page: int = 0,
    total_count: int = 0,
) -> InlineKeyboardMarkup:
    """
    Paginated market listing keyboard.
    Each challenge gets an [Accept] button, plus Prev/Next pagination.

    Args:
        challenges:   Page of Challenge objects (already sliced)
        page:         Current page index (0-based)
        total_count:  Total number of open challenges (for pagination)
    """
    buttons: list[list[InlineKeyboardButton]] = []

    for challenge in challenges:
        match = challenge.match
        label = (
            f"{match.home_team} vs {match.away_team} "
            f"· {challenge.amount_stars}⭐ · "
            f"{'🏠' if challenge.creator_side == 'home' else '✈️' if challenge.creator_side == 'away' else '🤝'}"
        )
        if len(label) > 50:
            label = label[:47] + "…"
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"view_challenge_{challenge.uuid}",
            ),
            InlineKeyboardButton(
                "✅ Accept",
                callback_data=f"accept_{challenge.uuid}",
            ),
        ])

    # Pagination row
    total_pages = math.ceil(total_count / MARKET_PAGE_SIZE) if total_count > 0 else 1
    nav_row: list[InlineKeyboardButton] = []

    if page > 0:
        nav_row.append(
            InlineKeyboardButton("⬅️ Prev", callback_data=f"market_page_{page - 1}")
        )

    nav_row.append(
        InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop")
    )

    if (page + 1) * MARKET_PAGE_SIZE < total_count:
        nav_row.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"market_page_{page + 1}")
        )

    if nav_row:
        buttons.append(nav_row)

    # Refresh button
    buttons.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="market_page_0"),
    ])

    return InlineKeyboardMarkup(buttons)


def accept_challenge_keyboard(challenge_uuid: str) -> InlineKeyboardMarkup:
    """
    Keyboard shown when viewing a challenge via invite link or market.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Accept This Challenge",
                callback_data=f"accept_{challenge_uuid}",
            ),
        ],
        [
            InlineKeyboardButton("❌ Decline", callback_data="decline"),
        ],
    ])


# ---------------------------------------------------------------------------
# Help & FAQ
# ---------------------------------------------------------------------------

def help_keyboard() -> InlineKeyboardMarkup:
    """Help screen keyboard with FAQ toggle."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❓ Is this gambling?", callback_data="faq_gambling"),
        ],
        [
            InlineKeyboardButton("⭐ How do Stars work?", callback_data="faq_stars"),
            InlineKeyboardButton("🏆 Create Challenge", callback_data="create_challenge"),
        ],
    ])


def faq_back_keyboard() -> InlineKeyboardMarkup:
    """Back button shown after displaying a FAQ answer."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Help", callback_data="back_to_help")],
    ])


# ---------------------------------------------------------------------------
# Generic
# ---------------------------------------------------------------------------

def cancel_keyboard() -> InlineKeyboardMarkup:
    """Single-button keyboard with just a cancel option."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Return to main menu button."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])
