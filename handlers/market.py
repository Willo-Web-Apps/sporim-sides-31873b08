"""
handlers/market.py — "The Market" — public open challenges list with pagination.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import get_db
from services.challenge_service import count_market_challenges, get_open_market_challenges

logger = logging.getLogger(__name__)

PER_PAGE = 5


async def show_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /market command or Market button callback."""
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        page = int(query.data.replace("market_page_", ""))
        edit = True
    else:
        page = 0
        edit = False

    async with get_db() as session:
        challenges = await get_open_market_challenges(session, page=page, per_page=PER_PAGE)
        total = await count_market_challenges(session)

    if not challenges:
        text = (
            "📋 *The Market*\n\n"
            "No open public challenges right now.\n"
            "Be the first — create one with /newchallenge!"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏆 Create Challenge", callback_data="new_challenge")]
        ])
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    lines = [f"📋 *The Market* — {total} open challenge{'s' if total != 1 else ''}\n"]
    buttons = []

    for c in challenges:
        match = c.match
        creator = c.creator
        creator_name = f"@{creator.username}" if creator.username else creator.first_name
        side_label = (
            match.home_team if c.creator_side == "home"
            else match.away_team if c.creator_side == "away"
            else "Draw"
        )
        line = (
            f"• *{match.home_team} vs {match.away_team}* | "
            f"{side_label} side | {c.amount_stars}⭐ | {creator_name}"
        )
        lines.append(line)
        buttons.append([
            InlineKeyboardButton(
                f"Accept — {c.amount_stars}⭐ ({match.home_team} vs {match.away_team})",
                callback_data=f"accept_{c.uuid}",
            )
        ])

    # Pagination
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"market_page_{page - 1}"))
    if (page + 1) * PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"market_page_{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("🏆 New Challenge", callback_data="new_challenge")])

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(buttons)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def accept_challenge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Accept button — show challenge details and send Stars invoice."""
    from services.challenge_service import get_challenge_by_uuid

    query = update.callback_query
    await query.answer()

    uuid = query.data.replace("accept_", "")
    acceptor = query.from_user

    async with get_db() as session:
        challenge = await get_challenge_by_uuid(session, uuid)

    if not challenge:
        await query.edit_message_text("❌ Challenge not found.")
        return

    if challenge.status != "open":
        await query.edit_message_text(
            f"This challenge is no longer open (status: {challenge.status})."
        )
        return

    if acceptor.id == challenge.creator.telegram_id:
        await query.answer("You can't accept your own challenge!", show_alert=True)
        return

    match = challenge.match
    creator = challenge.creator
    creator_name = f"@{creator.username}" if creator.username else creator.first_name

    await query.edit_message_text(
        f"⚡ *Accepting challenge from {creator_name}*\n\n"
        f"🏟️ {match.home_team} vs {match.away_team}\n"
        f"They're on: *{challenge.creator_side.upper()}*\n"
        f"You get: *{challenge.acceptor_side.upper()}*\n"
        f"💰 Stake: *{challenge.amount_stars} ⭐ each*\n\n"
        f"Lock your Stars to confirm:",
        parse_mode="Markdown",
    )

    await query.message.reply_invoice(
        title=f"Lock {challenge.amount_stars} ⭐ Stars",
        description=(
            f"{match.home_team} vs {match.away_team} — "
            f"Your side: {challenge.acceptor_side.capitalize()}"
        ),
        payload=f"acceptor_{uuid}",
        currency="XTR",
        prices=[{"label": "Stake", "amount": challenge.amount_stars}],
    )
