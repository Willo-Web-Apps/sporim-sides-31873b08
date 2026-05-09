"""
handlers/start.py — SideUp Bot Start & Deep-Link Handler
==========================================================
Handles the /start command in two modes:

  1. /start (no args)
     → Welcome message with main menu inline keyboard.

  2. /start ref_<uuid>
     → Challenge invite deep-link. Shows challenge details with an
       [Accept] button so the invited user can take the opposite side.

Also handles the "main_menu", "how_it_works", "faq_gambling", and
"faq_stars" callback queries that originate from the welcome screen.
"""

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import BOT_USERNAME
from db import get_db
from services import challenge_service
from utils.formatters import (
    format_challenge_card,
    format_how_it_works,
    format_welcome,
)
from utils.keyboards import (
    accept_challenge_keyboard,
    back_to_menu_keyboard,
    faq_back_keyboard,
    help_keyboard,
    main_menu_keyboard,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /start — entry point
# ---------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /start command.

    If a deep-link payload is present (args[0]), route to the challenge
    invite handler. Otherwise show the standard welcome screen.

    Args:
        update:  Incoming Telegram update.
        context: PTB context carrying args, bot, user_data, etc.
    """
    user = update.effective_user
    if user is None:
        return

    # Upsert the user record so we always have an up-to-date profile.
    async with get_db() as session:
        await challenge_service.get_or_create_user(
            session=session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name or "",
        )

    # Check for deep-link payload: /start ref_<uuid>
    if context.args:
        payload = context.args[0]
        if payload.startswith("ref_"):
            challenge_uuid = payload[4:]  # Strip the "ref_" prefix
            await _show_challenge_invite(update, context, challenge_uuid)
            return

    # Standard welcome screen
    await _send_welcome(update, context)


async def _send_welcome(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    edit: bool = False,
) -> None:
    """
    Send (or edit) the welcome message with the main menu keyboard.

    Args:
        update:  Incoming update.
        context: PTB context.
        edit:    If True, edit the existing message (for callback re-entry).
    """
    user = update.effective_user
    first_name = user.first_name if user else "there"

    text = format_welcome(first_name)
    keyboard = main_menu_keyboard()

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    else:
        message = update.message or (
            update.callback_query.message if update.callback_query else None
        )
        if message:
            await message.reply_text(
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )


async def _show_challenge_invite(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    challenge_uuid: str,
) -> None:
    """
    Render a challenge card from a deep-link invite and prompt acceptance.

    If the challenge no longer exists or is not open, show an appropriate
    error message with a menu button.

    Args:
        update:          Incoming update.
        context:         PTB context.
        challenge_uuid:  UUID extracted from the deep-link payload.
    """
    message = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if message is None:
        return

    async with get_db() as session:
        challenge = await challenge_service.get_challenge_by_uuid(session, challenge_uuid)

    if challenge is None:
        await message.reply_text(
            "❌ <b>Challenge not found.</b>\n\n"
            "This invite link may be invalid or the challenge was deleted.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    if challenge.status != "open":
        status_map = {
            "locked": "already accepted and locked 🔒",
            "resolved": "already resolved ✅",
            "cancelled": "cancelled ❌",
            "expired": "expired ⌛",
        }
        human = status_map.get(challenge.status, challenge.status)
        await message.reply_text(
            f"⚠️ <b>This challenge is {human}.</b>\n\n"
            "It's no longer available. Check The Market for open challenges!",
            reply_markup=back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    card = format_challenge_card(challenge, challenge.match)
    keyboard = accept_challenge_keyboard(challenge_uuid)

    await message.reply_text(
        text=card,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Callback query handlers (welcome-screen actions)
# ---------------------------------------------------------------------------


async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to the main menu from any screen."""
    query = update.callback_query
    await query.answer()
    await _send_welcome(update, context, edit=True)


async def cb_how_it_works(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the 'How It Works' explainer screen."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text=format_how_it_works(),
        reply_markup=help_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def cb_faq_gambling(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer the 'Is this gambling?' FAQ question."""
    query = update.callback_query
    await query.answer()
    text = (
        "❓ <b>Is SideUp gambling?</b>\n\n"
        "<b>No.</b> SideUp is a technology escrow platform.\n\n"
        "Here's the difference:\n"
        "• A bookmaker takes your money and gives you odds based on risk.\n"
        "• SideUp just holds equal funds between two private individuals "
        "and releases them automatically when a public sports result is confirmed.\n\n"
        "We don't set odds. We don't take risk. We don't profit from your outcome.\n\n"
        "Think of us as <b>PayPal for a bet between friends</b> — we're the "
        "trusted middle layer that makes sure both parties follow through.\n\n"
        "<i>Users are responsible for compliance with their local laws. "
        "SideUp is a technology service provider, not a gambling operator.</i>"
    )
    await query.edit_message_text(
        text=text,
        reply_markup=faq_back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def cb_faq_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain how Telegram Stars work in this context."""
    query = update.callback_query
    await query.answer()
    text = (
        "⭐ <b>How do Telegram Stars work?</b>\n\n"
        "Telegram Stars (XTR) are Telegram's built-in virtual currency, "
        "purchased through the Telegram app via Apple Pay, Google Pay, or card.\n\n"
        "<b>In SideUp:</b>\n"
        "1. When you create or accept a challenge, you pay the agreed amount "
        "of Stars into escrow.\n"
        "2. Stars sit locked in the challenge until the match ends.\n"
        "3. The winner receives the full pot (both sides) automatically.\n\n"
        "💡 Stars are real value — they can be used across Telegram services.\n\n"
        "🔒 <b>Your Stars are safe.</b> We use Telegram's native payment system "
        "with cryptographic charge IDs, making every transaction verifiable."
    )
    await query.edit_message_text(
        text=text,
        reply_markup=faq_back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def cb_back_to_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return from a FAQ answer back to the Help screen."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text=format_how_it_works(),
        reply_markup=help_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def cb_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User declined a challenge invite — show polite dismissal."""
    query = update.callback_query
    await query.answer("No problem! Challenges come and go. 😄")
    await query.edit_message_text(
        text=(
            "👍 No worries! You declined this challenge.\n\n"
            "Browse The Market for other open challenges, "
            "or create your own!"
        ),
        reply_markup=back_to_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """No-op handler for purely informational buttons (e.g. page count display)."""
    query = update.callback_query
    await query.answer()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app: Application) -> None:
    """
    Register all start-related handlers with the Application.

    Call this from main.py during setup.

    Args:
        app: The PTB Application instance.
    """
    # /start command — handles both cold start and deep links
    app.add_handler(CommandHandler("start", start_command))

    # Welcome screen callback actions
    app.add_handler(CallbackQueryHandler(cb_main_menu, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(cb_how_it_works, pattern="^how_it_works$"))
    app.add_handler(CallbackQueryHandler(cb_faq_gambling, pattern="^faq_gambling$"))
    app.add_handler(CallbackQueryHandler(cb_faq_stars, pattern="^faq_stars$"))
    app.add_handler(CallbackQueryHandler(cb_back_to_help, pattern="^back_to_help$"))
    app.add_handler(CallbackQueryHandler(cb_decline, pattern="^decline$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern="^noop$"))

    logger.info("Start handlers registered.")
