"""
handlers/challenge.py — SideUp Bot Challenge Creation Flow
============================================================
A ConversationHandler drives the full challenge-creation wizard:

    PICK_SPORT → PICK_LEAGUE → PICK_MATCH → PICK_SIDE
                → PICK_AMOUNT → PICK_VISIBILITY → CONFIRM

Entry points:
    - Callback "create_challenge" from main menu
    - /newchallenge command

The wizard stores intermediate state in context.user_data under
the key "challenge_draft". On confirmation, it calls challenge_service
to persist the record and displays the invite link.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import BOT_USERNAME, MAX_CHALLENGE_AMOUNT, MIN_CHALLENGE_AMOUNT
from db import get_db
from services import challenge_service, sports_api as sports_api_service
from utils.formatters import format_challenge_card, format_match
from utils.keyboards import (
    back_to_menu_keyboard,
    cancel_keyboard,
    confirm_challenge_keyboard,
    matches_keyboard,
    sides_keyboard,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation state constants
# ---------------------------------------------------------------------------

(
    PICK_SPORT,
    PICK_LEAGUE,
    PICK_MATCH,
    PICK_SIDE,
    PICK_AMOUNT,
    PICK_VISIBILITY,
    CONFIRM,
) = range(7)

# Key used inside context.user_data to hold draft state
_DRAFT = "challenge_draft"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sport_keyboard() -> InlineKeyboardMarkup:
    """Top-level sport picker: Football or Basketball."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚽ Football", callback_data="sport_football"),
            InlineKeyboardButton("🏀 Basketball", callback_data="sport_basketball"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_conversation")],
    ])


def _football_league_keyboard() -> InlineKeyboardMarkup:
    """League selection for Football."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League", callback_data="league_PL")],
        [
            InlineKeyboardButton("⭐ Champions League", callback_data="league_CL"),
            InlineKeyboardButton("🇪🇸 La Liga", callback_data="league_LL"),
        ],
        [InlineKeyboardButton("🇮🇹 Serie A", callback_data="league_SA")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_conversation")],
    ])


def _basketball_league_keyboard() -> InlineKeyboardMarkup:
    """League selection for Basketball (NBA only for now)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏀 NBA", callback_data="league_NBA")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_conversation")],
    ])


def _visibility_keyboard() -> InlineKeyboardMarkup:
    """Private invite link vs. public Market listing."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔒 Private (invite link)",
                callback_data="visibility_private",
            ),
        ],
        [
            InlineKeyboardButton(
                "🌍 Public (The Market)",
                callback_data="visibility_public",
            ),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_conversation")],
    ])


def _build_confirm_text(draft: dict) -> str:
    """Build the human-readable confirmation summary from the draft dict."""
    match_data = draft["match_data"]
    side = draft["side"]
    amount = draft["amount_stars"]
    is_public = draft.get("is_public", False)

    home = match_data["home_team"]
    away = match_data["away_team"]
    kickoff: datetime = match_data["kickoff_time"]
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    kickoff_str = kickoff.strftime("%-d %b %H:%M UTC")

    side_emoji = {"home": "🏠", "away": "✈️", "draw": "🤝"}.get(side, "❓")
    side_label = {"home": home, "away": away, "draw": "Draw"}.get(side, side)
    visibility_str = "🌍 Public (anyone in The Market can accept)" if is_public else "🔒 Private (only via invite link)"

    return (
        "✅ <b>Confirm your challenge</b>\n\n"
        f"⚽ <b>Match:</b> {home} vs {away}\n"
        f"📅 <b>Kickoff:</b> {kickoff_str}\n\n"
        f"{side_emoji} <b>Your side:</b> {side_label}\n"
        f"⭐ <b>Each side stakes:</b> {amount:,} Stars\n"
        f"🏆 <b>Total pot:</b> {amount * 2:,} Stars\n\n"
        f"👁 <b>Visibility:</b> {visibility_str}\n\n"
        "<i>By confirming, you authorise SideUp to collect your Stars "
        "via Telegram Stars payment. You'll be asked to pay after confirming.</i>"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def entry_create_challenge(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Entry point for challenge creation — triggered by callback or /newchallenge.
    Clears any stale draft and asks the user to pick a sport.
    """
    # Clear stale draft
    if context.user_data is not None:
        context.user_data.pop(_DRAFT, None)

    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            text=(
                "🏆 <b>New Challenge</b>\n\n"
                "Which sport would you like to challenge on?"
            ),
            reply_markup=_sport_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            text=(
                "🏆 <b>New Challenge</b>\n\n"
                "Which sport would you like to challenge on?"
            ),
            reply_markup=_sport_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    return PICK_SPORT


# ---------------------------------------------------------------------------
# PICK_SPORT
# ---------------------------------------------------------------------------


async def pick_sport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User selected Football or Basketball — show league options."""
    query = update.callback_query
    await query.answer()

    sport = query.data  # "sport_football" or "sport_basketball"

    if context.user_data is None:
        context.user_data = {}
    context.user_data[_DRAFT] = {"sport": sport}

    if sport == "sport_football":
        await query.edit_message_text(
            text="⚽ <b>Football</b>\n\nPick a league:",
            reply_markup=_football_league_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.edit_message_text(
            text="🏀 <b>Basketball</b>\n\nPick a league:",
            reply_markup=_basketball_league_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    return PICK_LEAGUE


# ---------------------------------------------------------------------------
# PICK_LEAGUE
# ---------------------------------------------------------------------------


async def pick_league(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User selected a league — fetch upcoming matches and display them."""
    query = update.callback_query
    await query.answer()

    league_code = query.data.replace("league_", "")  # e.g. "PL", "NBA"

    draft: dict = context.user_data.get(_DRAFT, {})
    draft["league"] = league_code
    context.user_data[_DRAFT] = draft

    await query.edit_message_text(
        text=f"⏳ <b>Fetching upcoming matches…</b>",
        parse_mode=ParseMode.HTML,
    )

    # Fetch matches from the sports API
    matches = await sports_api_service.get_upcoming_matches(league_code)

    # Filter to only upcoming (scheduled) matches, cap at 5
    now = datetime.now(timezone.utc)
    upcoming = [
        m for m in matches
        if m.get("kickoff_time") and m["kickoff_time"] > now
    ][:5]

    if not upcoming:
        await query.edit_message_text(
            text=(
                "😔 <b>No upcoming matches found</b>\n\n"
                "There are no scheduled matches in this league for the next 7 days.\n"
                "Try a different league!"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back", callback_data="back_to_sport")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_conversation")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return PICK_LEAGUE

    # Store matches list in draft for reference in later steps
    draft["available_matches"] = upcoming
    context.user_data[_DRAFT] = draft

    league_names = {
        "PL": "Premier League",
        "CL": "Champions League",
        "LL": "La Liga",
        "SA": "Serie A",
        "NBA": "NBA",
    }
    league_label = league_names.get(league_code, league_code)

    keyboard = matches_keyboard(upcoming, league_code)
    await query.edit_message_text(
        text=(
            f"📅 <b>{league_label} — Upcoming Matches</b>\n\n"
            "Pick the match you want to challenge on:"
        ),
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )

    return PICK_MATCH


# ---------------------------------------------------------------------------
# PICK_MATCH
# ---------------------------------------------------------------------------


async def pick_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User selected a match — ask which side they want to back."""
    query = update.callback_query
    await query.answer()

    external_id = query.data.replace("match_", "")

    draft: dict = context.user_data.get(_DRAFT, {})
    available = draft.get("available_matches", [])

    # Find the selected match dict
    match_data: Optional[dict] = next(
        (m for m in available if m["external_id"] == external_id), None
    )

    if match_data is None:
        await query.answer("Match not found — please restart.", show_alert=True)
        return ConversationHandler.END

    draft["match_data"] = match_data
    context.user_data[_DRAFT] = draft

    kickoff: datetime = match_data["kickoff_time"]
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    kickoff_str = kickoff.strftime("%-d %b %H:%M UTC")

    home = match_data["home_team"]
    away = match_data["away_team"]

    await query.edit_message_text(
        text=(
            f"⚽ <b>{home}  vs  {away}</b>\n"
            f"📅 {kickoff_str}\n\n"
            "Which side are you backing?"
        ),
        reply_markup=sides_keyboard(home, away),
        parse_mode=ParseMode.HTML,
    )

    return PICK_SIDE


# ---------------------------------------------------------------------------
# PICK_SIDE
# ---------------------------------------------------------------------------


async def pick_side(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked home/draw/away — ask for the stake amount."""
    query = update.callback_query
    await query.answer()

    side = query.data.replace("side_", "")  # "home", "draw", or "away"

    draft: dict = context.user_data.get(_DRAFT, {})
    draft["side"] = side
    context.user_data[_DRAFT] = draft

    match_data = draft.get("match_data", {})
    home = match_data.get("home_team", "Home")
    away = match_data.get("away_team", "Away")
    side_label = {"home": f"🏠 {home}", "away": f"✈️ {away}", "draw": "🤝 Draw"}.get(side, side)

    await query.edit_message_text(
        text=(
            f"✅ <b>You're backing:</b> {side_label}\n\n"
            f"How many ⭐ Stars do you want each side to stake?\n\n"
            f"<i>Min: {MIN_CHALLENGE_AMOUNT} · Max: {MAX_CHALLENGE_AMOUNT:,}</i>\n\n"
            "Type the number of Stars:"
        ),
        reply_markup=cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )

    return PICK_AMOUNT


# ---------------------------------------------------------------------------
# PICK_AMOUNT
# ---------------------------------------------------------------------------


async def pick_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed an amount — validate and ask for visibility."""
    raw = update.message.text.strip()

    # Parse and validate
    try:
        amount = int(raw)
    except ValueError:
        await update.message.reply_text(
            f"⚠️ Please enter a whole number (e.g. <b>50</b>).",
            reply_markup=cancel_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return PICK_AMOUNT

    if amount < MIN_CHALLENGE_AMOUNT or amount > MAX_CHALLENGE_AMOUNT:
        await update.message.reply_text(
            f"⚠️ Amount must be between <b>{MIN_CHALLENGE_AMOUNT}</b> "
            f"and <b>{MAX_CHALLENGE_AMOUNT:,}</b> Stars.\n\nPlease try again:",
            reply_markup=cancel_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return PICK_AMOUNT

    draft: dict = context.user_data.get(_DRAFT, {})
    draft["amount_stars"] = amount
    context.user_data[_DRAFT] = draft

    await update.message.reply_text(
        text=(
            f"⭐ <b>{amount:,} Stars</b> each side.\n\n"
            "Should this challenge be private (invite link only) "
            "or public (listed in The Market for anyone to accept)?"
        ),
        reply_markup=_visibility_keyboard(),
        parse_mode=ParseMode.HTML,
    )

    return PICK_VISIBILITY


# ---------------------------------------------------------------------------
# PICK_VISIBILITY
# ---------------------------------------------------------------------------


async def pick_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User chose private or public — show the confirmation summary."""
    query = update.callback_query
    await query.answer()

    is_public = query.data == "visibility_public"

    draft: dict = context.user_data.get(_DRAFT, {})
    draft["is_public"] = is_public
    context.user_data[_DRAFT] = draft

    confirm_text = _build_confirm_text(draft)

    await query.edit_message_text(
        text=confirm_text,
        reply_markup=confirm_challenge_keyboard(),
        parse_mode=ParseMode.HTML,
    )

    return CONFIRM


# ---------------------------------------------------------------------------
# CONFIRM — create the challenge
# ---------------------------------------------------------------------------


async def confirm_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    User confirmed. Create the challenge record and send the invite link.

    Steps:
        1. Upsert user
        2. Upsert match record (from draft data)
        3. Create challenge in 'open' status
        4. Trigger Stars payment invoice so the creator locks their stake
        5. Display invite link
    """
    query = update.callback_query
    await query.answer()

    tg_user = update.effective_user
    if tg_user is None:
        await query.edit_message_text("❌ Could not identify your account. Please try again.")
        return ConversationHandler.END

    draft: dict = context.user_data.get(_DRAFT, {})
    match_data = draft.get("match_data")

    if not match_data or "side" not in draft or "amount_stars" not in draft:
        await query.edit_message_text(
            "❌ <b>Session expired.</b>\n\nPlease start over with /newchallenge.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    await query.edit_message_text(
        "⏳ <b>Creating your challenge…</b>",
        parse_mode=ParseMode.HTML,
    )

    try:
        async with get_db() as session:
            # 1. Upsert user
            user = await challenge_service.get_or_create_user(
                session=session,
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name or "",
            )

            # 2. Upsert match
            match = await challenge_service.get_or_create_match(session, match_data)

            # 3. Create challenge
            challenge = await challenge_service.create_challenge(
                session=session,
                creator=user,
                match=match,
                creator_side=draft["side"],
                amount_stars=draft["amount_stars"],
                is_public=draft.get("is_public", False),
            )

        # 4. Invite link
        invite_link = f"https://t.me/{BOT_USERNAME}?start=ref_{challenge.uuid}"

    except ValueError as e:
        await query.edit_message_text(
            f"❌ <b>Could not create challenge:</b>\n{e}\n\nPlease try again.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END
    except Exception as e:
        logger.exception("Unexpected error creating challenge: %s", e)
        await query.edit_message_text(
            "❌ <b>Something went wrong.</b> Please try again in a moment.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    # 5. Show success + invite link
    match_label = f"{match_data['home_team']} vs {match_data['away_team']}"
    side_label = {
        "home": f"🏠 {match_data['home_team']}",
        "away": f"✈️ {match_data['away_team']}",
        "draw": "🤝 Draw",
    }.get(draft["side"], draft["side"])

    success_text = (
        "🎉 <b>Challenge created!</b>\n\n"
        f"<b>Match:</b> {match_label}\n"
        f"<b>Your side:</b> {side_label}\n"
        f"<b>Stake:</b> {draft['amount_stars']:,} ⭐ each side\n\n"
        "📤 <b>Share this invite link with your friend:</b>\n"
        f"<code>{invite_link}</code>\n\n"
        "Your friend clicks the link, accepts, and both sides lock Stars — "
        "then we watch the match and pay out the winner automatically.\n\n"
        "⚠️ <i>Next step: pay your stake via Stars to lock in your side.</i>\n"
        f"Use /pay_{challenge.uuid[:8]} or tap the button below."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⭐ Pay My Stake Now",
                callback_data=f"pay_creator_{challenge.uuid}",
            )
        ],
        [
            InlineKeyboardButton("📤 Share Link", url=invite_link),
        ],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])

    await query.edit_message_text(
        text=success_text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )

    # Clear draft
    if context.user_data:
        context.user_data.pop(_DRAFT, None)

    return ConversationHandler.END


async def cancel_conversation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel the challenge creation wizard."""
    query = update.callback_query
    if query:
        await query.answer("Cancelled.")
        await query.edit_message_text(
            "❌ <b>Challenge creation cancelled.</b>\n\nNo Stars were charged.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "❌ <b>Challenge creation cancelled.</b>",
            reply_markup=back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    if context.user_data:
        context.user_data.pop(_DRAFT, None)

    return ConversationHandler.END


async def cancel_via_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel command mid-conversation."""
    if context.user_data:
        context.user_data.pop(_DRAFT, None)
    await update.message.reply_text(
        "❌ Challenge creation cancelled.",
        reply_markup=back_to_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app: Application) -> None:
    """
    Register the challenge creation ConversationHandler with the Application.

    Args:
        app: The PTB Application instance.
    """
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_create_challenge, pattern="^create_challenge$"),
            CommandHandler("newchallenge", entry_create_challenge),
        ],
        states={
            PICK_SPORT: [
                CallbackQueryHandler(pick_sport, pattern="^sport_(football|basketball)$"),
                CallbackQueryHandler(cancel_conversation, pattern="^cancel_conversation$"),
            ],
            PICK_LEAGUE: [
                CallbackQueryHandler(pick_league, pattern="^league_"),
                CallbackQueryHandler(
                    entry_create_challenge, pattern="^back_to_sport$"
                ),
                CallbackQueryHandler(cancel_conversation, pattern="^cancel_conversation$"),
            ],
            PICK_MATCH: [
                CallbackQueryHandler(pick_match, pattern="^match_"),
                CallbackQueryHandler(cancel_conversation, pattern="^cancel_conversation$"),
            ],
            PICK_SIDE: [
                CallbackQueryHandler(pick_side, pattern="^side_(home|draw|away)$"),
                CallbackQueryHandler(cancel_conversation, pattern="^cancel_conversation$"),
            ],
            PICK_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_amount),
                CallbackQueryHandler(cancel_conversation, pattern="^cancel_conversation$"),
                CommandHandler("cancel", cancel_via_text),
            ],
            PICK_VISIBILITY: [
                CallbackQueryHandler(
                    pick_visibility, pattern="^visibility_(private|public)$"
                ),
                CallbackQueryHandler(cancel_conversation, pattern="^cancel_conversation$"),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_create, pattern="^confirm_create$"),
                CallbackQueryHandler(cancel_conversation, pattern="^cancel_conversation$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_via_text),
            CallbackQueryHandler(cancel_conversation, pattern="^cancel_conversation$"),
        ],
        allow_reentry=True,
        name="challenge_creation",
        persistent=False,
    )

    app.add_handler(conv_handler)
    logger.info("Challenge creation ConversationHandler registered.")
