"""
handlers/market.py — SideUp Bot Market Handler
================================================
"The Market" is a paginated listing of open public challenges that any
user can accept. This module handles:

    - /market command
    - "open_market" callback query (main menu button)
    - "market_page_<n>" pagination callbacks
    - "view_challenge_<uuid>" — show a single challenge card
    - "accept_<uuid>" — initiate the payment flow for the acceptor

The actual payment invoice is sent from here; the payment handler in
handlers/payment.py processes the pre-checkout and successful_payment
events from Telegram.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from db import get_db
from services import challenge_service
from utils.formatters import format_challenge_card, format_market_listing
from utils.keyboards import (
    accept_challenge_keyboard,
    back_to_menu_keyboard,
    market_keyboard,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


async def _render_market(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    edit: bool = False,
) -> None:
    """
    Fetch and display the paginated market listing.

    Args:
        update:  Incoming Telegram update.
        context: PTB context.
        page:    Zero-based page index.
        edit:    If True, edit the existing message instead of sending a new one.
    """
    async with get_db() as session:
        challenges, total = await challenge_service.get_open_market_challenges(
            session=session,
            page=page,
        )

    text = format_market_listing(challenges)
    keyboard = market_keyboard(challenges, page=page, total_count=total)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    else:
        target_message = update.message or (
            update.callback_query.message if update.callback_query else None
        )
        if target_message:
            await target_message.reply_text(
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def market_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /market command — show the first page of open challenges."""
    await _render_market(update, context, page=0, edit=False)


async def cb_open_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback handler for the 'open_market' button on the welcome screen."""
    query = update.callback_query
    await query.answer()
    await _render_market(update, context, page=0, edit=True)


async def cb_market_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle pagination callbacks: "market_page_<n>".

    Extracts the page number from the callback data and re-renders the market.
    """
    query = update.callback_query
    await query.answer()

    try:
        page = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        page = 0

    await _render_market(update, context, page=page, edit=True)


async def cb_view_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Show a detailed card for a single challenge.

    Triggered by tapping a challenge row in the market listing.
    Pattern: "view_challenge_<uuid>"
    """
    query = update.callback_query
    await query.answer()

    challenge_uuid = query.data.replace("view_challenge_", "")

    async with get_db() as session:
        challenge = await challenge_service.get_challenge_by_uuid(session, challenge_uuid)

    if challenge is None or challenge.status != "open":
        await query.edit_message_text(
            "⚠️ <b>This challenge is no longer available.</b>\n\n"
            "It may have just been accepted by someone else. Refresh to see the latest!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Back to Market", callback_data="open_market")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return

    card = format_challenge_card(challenge, challenge.match)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Accept This Challenge",
                callback_data=f"accept_{challenge_uuid}",
            ),
        ],
        [InlineKeyboardButton("⬅️ Back to Market", callback_data="open_market")],
    ])

    await query.edit_message_text(
        text=card,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def cb_accept_challenge(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    User wants to accept a challenge.

    Pattern: "accept_<uuid>"

    Steps:
        1. Load and validate the challenge (open, user ≠ creator)
        2. Record the acceptor on the challenge
        3. Send a Stars payment invoice for the stake
           (payment handler finalises the lock on successful_payment)
    """
    query = update.callback_query
    await query.answer()

    tg_user = update.effective_user
    if tg_user is None:
        await query.answer("Could not identify your account.", show_alert=True)
        return

    challenge_uuid = query.data.replace("accept_", "")

    async with get_db() as session:
        # Upsert acceptor user
        acceptor = await challenge_service.get_or_create_user(
            session=session,
            telegram_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name or "",
        )

        try:
            challenge = await challenge_service.accept_challenge(
                session=session,
                challenge_uuid=challenge_uuid,
                acceptor=acceptor,
            )
        except ValueError as e:
            await query.answer(str(e), show_alert=True)
            return

        match = challenge.match
        creator_name = challenge.creator.display_name() if challenge.creator else "them"
        amount = challenge.amount_stars

        # Determine acceptor's side label
        acceptor_side = challenge.acceptor_side or "against_draw"
        home = match.home_team
        away = match.away_team
        side_label = {
            "home": f"🏠 {home}",
            "away": f"✈️ {away}",
            "against_draw": "🤝 Either team wins (not a draw)",
        }.get(acceptor_side, acceptor_side)

    # Prompt payment for the stake
    invoice_text = (
        f"✅ <b>Challenge Accepted!</b>\n\n"
        f"<b>{home} vs {away}</b>\n"
        f"You're taking: {side_label}\n"
        f"vs {creator_name}\n\n"
        f"Stake: <b>{amount:,} ⭐ Stars</b>\n\n"
        "Tap below to lock your Stars and make it official!"
    )

    payment_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"⭐ Pay {amount:,} Stars",
                callback_data=f"pay_acceptor_{challenge_uuid}",
            )
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="open_market")],
    ])

    await query.edit_message_text(
        text=invoice_text,
        reply_markup=payment_keyboard,
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app: Application) -> None:
    """
    Register market handlers with the Application.

    Args:
        app: The PTB Application instance.
    """
    app.add_handler(CommandHandler("market", market_command))
    app.add_handler(CallbackQueryHandler(cb_open_market, pattern="^open_market$"))
    app.add_handler(CallbackQueryHandler(cb_market_page, pattern=r"^market_page_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_view_challenge, pattern="^view_challenge_"))
    app.add_handler(CallbackQueryHandler(cb_accept_challenge, pattern="^accept_"))

    logger.info("Market handlers registered.")
