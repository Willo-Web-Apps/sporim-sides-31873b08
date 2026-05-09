"""
handlers/challenge.py — ConversationHandler for creating a new P2P challenge.
States: PICK_SPORT → PICK_LEAGUE → PICK_MATCH → PICK_SIDE → PICK_AMOUNT → PICK_VISIBILITY → CONFIRM
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import LEAGUE_CODES, MAX_CHALLENGE_AMOUNT, MIN_CHALLENGE_AMOUNT
from db import get_db
from services.challenge_service import create_challenge
from services.sports_api import BallDontLieClient, FootballDataClient, upsert_matches_to_db

logger = logging.getLogger(__name__)

# Conversation states
(
    PICK_SPORT,
    PICK_LEAGUE,
    PICK_MATCH,
    PICK_SIDE,
    PICK_AMOUNT,
    PICK_VISIBILITY,
    CONFIRM,
) = range(7)

CANCEL_TEXT = "❌ Challenge creation cancelled. /start to begin again."


# ── Entry point ───────────────────────────────────────────────────────────────

async def new_challenge_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: called from /newchallenge or the New Challenge button callback."""
    context.user_data.clear()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚽ Football", callback_data="sport_football"),
            InlineKeyboardButton("🏀 Basketball", callback_data="sport_basketball"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="challenge_cancel")],
    ])
    text = "🏆 *New Challenge*\n\nWhat sport do you want to challenge on?"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    return PICK_SPORT


# ── PICK_SPORT ────────────────────────────────────────────────────────────────

async def pick_sport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sport = query.data.replace("sport_", "")
    context.user_data["sport"] = sport

    if sport == "football":
        buttons = [
            [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League", callback_data="league_PL")],
            [InlineKeyboardButton("🏆 Champions League", callback_data="league_CL")],
            [InlineKeyboardButton("🇪🇸 La Liga", callback_data="league_PD")],
            [InlineKeyboardButton("🇮🇹 Serie A", callback_data="league_SA")],
            [InlineKeyboardButton("❌ Cancel", callback_data="challenge_cancel")],
        ]
    else:  # basketball
        buttons = [
            [InlineKeyboardButton("🏀 NBA", callback_data="league_NBA")],
            [InlineKeyboardButton("❌ Cancel", callback_data="challenge_cancel")],
        ]

    await query.edit_message_text(
        f"⚽ Sport: *{sport.capitalize()}*\n\nPick a league:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return PICK_LEAGUE


# ── PICK_LEAGUE ───────────────────────────────────────────────────────────────

async def pick_league(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    league_code = query.data.replace("league_", "")
    context.user_data["league"] = league_code

    await query.edit_message_text(
        f"⏳ Fetching upcoming matches for *{league_code}*…",
        parse_mode="Markdown",
    )

    # Fetch + cache matches
    async with get_db() as session:
        if league_code == "NBA":
            client = BallDontLieClient()
            raw = await client.get_games(days_ahead=7)
        else:
            client = FootballDataClient()
            raw = await client.get_matches(league_code, days_ahead=7)

        if not raw:
            await query.edit_message_text(
                "😕 No upcoming matches found for this league right now. Try another league or check back soon.\n\n/start"
            )
            return ConversationHandler.END

        await upsert_matches_to_db(session, raw[:10])

        # Load from DB (to get the DB IDs)
        from sqlalchemy import select
        from models import Match
        result = await session.execute(
            select(Match)
            .where(Match.league == league_code, Match.status == "scheduled")
            .order_by(Match.kickoff_time)
            .limit(5)
        )
        matches = list(result.scalars().all())

    if not matches:
        await query.edit_message_text(
            "😕 No upcoming matches found right now. Try again later.\n\n/start"
        )
        return ConversationHandler.END

    context.user_data["matches"] = {str(m.id): m for m in matches}

    buttons = []
    for m in matches:
        label = f"{m.home_team} vs {m.away_team} • {m.kickoff_time.strftime('%b %d')}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"match_{m.id}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="challenge_cancel")])

    await query.edit_message_text(
        f"📅 Upcoming *{league_code}* matches — pick one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return PICK_MATCH


# ── PICK_MATCH ────────────────────────────────────────────────────────────────

async def pick_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    match_id_str = query.data.replace("match_", "")
    matches = context.user_data.get("matches", {})
    match = matches.get(match_id_str)

    if not match:
        # Re-load from DB
        async with get_db() as session:
            from sqlalchemy import select
            from models import Match
            result = await session.execute(select(Match).where(Match.id == int(match_id_str)))
            match = result.scalar_one_or_none()

    if not match:
        await query.edit_message_text("Match not found. /start")
        return ConversationHandler.END

    context.user_data["match_id"] = int(match_id_str)
    context.user_data["match_home"] = match.home_team
    context.user_data["match_away"] = match.away_team

    buttons = [
        [InlineKeyboardButton(f"🏠 {match.home_team}", callback_data="side_home")],
        [InlineKeyboardButton("🤝 Draw", callback_data="side_draw")],
        [InlineKeyboardButton(f"✈️ {match.away_team}", callback_data="side_away")],
        [InlineKeyboardButton("❌ Cancel", callback_data="challenge_cancel")],
    ]
    await query.edit_message_text(
        f"🏟️ *{match.home_team} vs {match.away_team}*\n\nPick your side:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return PICK_SIDE


# ── PICK_SIDE ─────────────────────────────────────────────────────────────────

async def pick_side(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    side = query.data.replace("side_", "")
    context.user_data["creator_side"] = side

    home = context.user_data["match_home"]
    away = context.user_data["match_away"]
    side_label = home if side == "home" else away if side == "away" else "Draw"
    context.user_data["side_label"] = side_label

    await query.edit_message_text(
        f"✅ You picked *{side_label}*\n\n"
        f"How many ⭐ Stars do you want to stake?\n"
        f"_(Min: {MIN_CHALLENGE_AMOUNT} • Max: {MAX_CHALLENGE_AMOUNT})_\n\n"
        f"Type a number:",
        parse_mode="Markdown",
    )
    return PICK_AMOUNT


# ── PICK_AMOUNT ───────────────────────────────────────────────────────────────

async def pick_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        amount = int(text)
    except ValueError:
        await update.message.reply_text(
            f"⚠️ Please enter a whole number between {MIN_CHALLENGE_AMOUNT} and {MAX_CHALLENGE_AMOUNT}."
        )
        return PICK_AMOUNT

    if amount < MIN_CHALLENGE_AMOUNT or amount > MAX_CHALLENGE_AMOUNT:
        await update.message.reply_text(
            f"⚠️ Amount must be between {MIN_CHALLENGE_AMOUNT} and {MAX_CHALLENGE_AMOUNT} Stars."
        )
        return PICK_AMOUNT

    context.user_data["amount"] = amount

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔒 Private (invite link only)", callback_data="vis_private"),
            InlineKeyboardButton("🌍 Public (The Market)", callback_data="vis_public"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="challenge_cancel")],
    ])
    await update.message.reply_text(
        f"💰 Stake: *{amount} ⭐ Stars* each side\n\nShould this challenge be public or private?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return PICK_VISIBILITY


# ── PICK_VISIBILITY ───────────────────────────────────────────────────────────

async def pick_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    is_public = query.data == "vis_public"
    context.user_data["is_public"] = is_public

    d = context.user_data
    home = d["match_home"]
    away = d["match_away"]
    side_label = d["side_label"]
    amount = d["amount"]
    vis_label = "🌍 Public — anyone can accept" if is_public else "🔒 Private — invite link only"

    confirm_text = (
        f"📋 *Challenge Summary*\n\n"
        f"🏟️ Match: *{home} vs {away}*\n"
        f"🎯 Your side: *{side_label}*\n"
        f"💰 Stake: *{amount} ⭐ Stars each*\n"
        f"🏆 Winner takes: *{amount * 2} ⭐ Stars*\n"
        f"👁️ Visibility: {vis_label}\n\n"
        f"Confirm and lock your Stars?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Confirm — Lock {amount} ⭐", callback_data="confirm_challenge")],
        [InlineKeyboardButton("❌ Cancel", callback_data="challenge_cancel")],
    ])
    await query.edit_message_text(confirm_text, parse_mode="Markdown", reply_markup=keyboard)
    return CONFIRM


# ── CONFIRM ───────────────────────────────────────────────────────────────────

async def confirm_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    d = context.user_data
    user = query.from_user

    async with get_db() as session:
        challenge = await create_challenge(
            session=session,
            telegram_id=user.id,
            first_name=user.first_name,
            username=user.username,
            match_id=d["match_id"],
            creator_side=d["creator_side"],
            amount_stars=d["amount"],
            is_public=d["is_public"],
        )
        invite_link = challenge.invite_link
        uuid = challenge.uuid

    # Now request Stars payment from creator
    await query.edit_message_text(
        f"✅ Challenge created! Now lock your *{d['amount']} ⭐ Stars* to activate it:",
        parse_mode="Markdown",
    )

    # Send Stars invoice
    await query.message.reply_invoice(
        title=f"Lock {d['amount']} ⭐ Stars",
        description=(
            f"{d['match_home']} vs {d['match_away']} — "
            f"Your side: {d['side_label']}"
        ),
        payload=f"creator_{uuid}",
        currency="XTR",
        prices=[{"label": "Stake", "amount": d["amount"]}],
    )

    context.user_data["pending_invite_link"] = invite_link
    context.user_data["pending_uuid"] = uuid
    return ConversationHandler.END


async def cancel_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(CANCEL_TEXT)
    else:
        await update.message.reply_text(CANCEL_TEXT)
    context.user_data.clear()
    return ConversationHandler.END


# ── Build the ConversationHandler ─────────────────────────────────────────────

def build_challenge_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("newchallenge", new_challenge_entry),
            CallbackQueryHandler(new_challenge_entry, pattern="^new_challenge$"),
        ],
        states={
            PICK_SPORT: [CallbackQueryHandler(pick_sport, pattern="^sport_")],
            PICK_LEAGUE: [CallbackQueryHandler(pick_league, pattern="^league_")],
            PICK_MATCH: [CallbackQueryHandler(pick_match, pattern="^match_")],
            PICK_SIDE: [CallbackQueryHandler(pick_side, pattern="^side_")],
            PICK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pick_amount)],
            PICK_VISIBILITY: [CallbackQueryHandler(pick_visibility, pattern="^vis_")],
            CONFIRM: [
                CallbackQueryHandler(confirm_challenge, pattern="^confirm_challenge$"),
                CallbackQueryHandler(cancel_challenge, pattern="^challenge_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_challenge),
            CallbackQueryHandler(cancel_challenge, pattern="^challenge_cancel$"),
        ],
        per_user=True,
        per_chat=True,
    )
