"""
handlers/start.py — /start command handler + deep-link challenge previews.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import get_db
from services.challenge_service import get_challenge_by_uuid, get_or_create_user

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "👋 *Welcome to SideUp!*\n\n"
    "No house. Just you and them.\n"
    "Lock ⭐ Stars, pick a side — winner takes all.\n\n"
    "How it works:\n"
    "1️⃣ Pick a match & choose your side\n"
    "2️⃣ Send the challenge link to a friend\n"
    "3️⃣ Both lock Stars in escrow\n"
    "4️⃣ Bot checks the result automatically — winner paid instantly\n\n"
    "_We are a social escrow platform, not a betting operator._"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — with or without deep-link payload."""
    user = update.effective_user
    args = context.args  # list of strings after /start

    # Register or update user in DB
    async with get_db() as session:
        await get_or_create_user(session, user.id, user.first_name, user.username)

    # Deep-link: /start ref_{uuid}
    if args and args[0].startswith("ref_"):
        uuid = args[0][4:]  # strip "ref_"
        await _show_challenge_preview(update, context, uuid)
        return

    # Normal /start
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏆 New Challenge", callback_data="new_challenge"),
            InlineKeyboardButton("📋 The Market", callback_data="market_page_0"),
        ],
        [
            InlineKeyboardButton("📊 My Stats", callback_data="my_stats"),
        ],
    ])
    await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=keyboard)


async def _show_challenge_preview(
    update: Update, context: ContextTypes.DEFAULT_TYPE, uuid: str
) -> None:
    """Show a challenge invite preview with an Accept button."""
    async with get_db() as session:
        challenge = await get_challenge_by_uuid(session, uuid)

    if not challenge:
        await update.message.reply_text("❌ This challenge link is invalid or has expired.")
        return

    if challenge.status != "open":
        status_msgs = {
            "locked": "🔒 This challenge is already locked — both sides have paid.",
            "resolved": "✅ This challenge has already been resolved.",
            "expired": "⏰ This challenge has expired.",
            "cancelled": "❌ This challenge was cancelled.",
        }
        await update.message.reply_text(
            status_msgs.get(challenge.status, f"This challenge is {challenge.status}.")
        )
        return

    # Don't let the creator accept their own challenge
    if update.effective_user.id == challenge.creator.telegram_id:
        await update.message.reply_text(
            "🤔 This is your own challenge! Share the link with a friend so they can accept it."
        )
        return

    match = challenge.match
    creator = challenge.creator
    creator_name = f"@{creator.username}" if creator.username else creator.first_name

    text = (
        f"⚡ *Challenge from {creator_name}*\n\n"
        f"🏟️ *{match.home_team} vs {match.away_team}*\n"
        f"📅 {match.kickoff_time.strftime('%b %d, %H:%M UTC')}\n\n"
        f"They picked: *{challenge.creator_side.upper()}* ({match.home_team if challenge.creator_side == 'home' else match.away_team if challenge.creator_side == 'away' else 'Draw'})\n"
        f"You get: *{challenge.acceptor_side.upper()}*\n"
        f"💰 Stake: *{challenge.amount_stars} ⭐ Stars each*\n"
        f"🏆 Winner takes: *{challenge.pot_stars} ⭐ Stars*\n\n"
        f"Do you accept?"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"✅ Accept — Lock {challenge.amount_stars} ⭐",
                callback_data=f"accept_{uuid}",
            )
        ],
        [InlineKeyboardButton("❌ Decline", callback_data="decline_challenge")],
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for My Stats button."""
    from services.challenge_service import get_user_stats
    query = update.callback_query
    await query.answer()

    async with get_db() as session:
        stats = await get_user_stats(session, query.from_user.id)

    if not stats["found"]:
        await query.edit_message_text("You haven't made any challenges yet. Start with /start!")
        return

    text = (
        f"📊 *Your SideUp Stats*\n\n"
        f"Challenges created: {stats['created']}\n"
        f"Challenges accepted: {stats['accepted']}\n"
        f"Wins: {stats['wins']} 🏆\n\n"
        f"_Keep challenging — the Stars await!_"
    )
    await query.edit_message_text(text, parse_mode="Markdown")


async def decline_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User declined an invite."""
    query = update.callback_query
    await query.answer("No worries — declined.")
    await query.edit_message_text("You declined the challenge.")
