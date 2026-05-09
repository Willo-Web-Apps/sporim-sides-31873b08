"""
handlers/payment.py — SideUp Bot Telegram Stars Payment Flow
=============================================================
Handles the complete Stars payment lifecycle for challenge stakes:

  1. send_invoice  — bot sends a Stars invoice to the user
  2. pre_checkout_query  — Telegram asks us to approve/reject before charging
  3. successful_payment  — Telegram confirms money received; we lock Stars

Also handles:
  - "pay_creator_<uuid>"  callback  — send invoice to the challenge creator
  - "pay_acceptor_<uuid>" callback  — send invoice to the challenge acceptor
  - Refund utility function used by admin and scheduler

Telegram Stars notes:
  - Currency code is "XTR" (not "USD" or "EUR")
  - Prices are in the smallest Stars unit (1 Star = 1 unit)
  - Telegram does not charge users until AnswerPreCheckoutQuery(ok=True)
  - Refunds use refundStarPayment(user_id, telegram_payment_charge_id)
"""

import logging

from telegram import LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from config import BOT_USERNAME
from db import get_db
from services import challenge_service
from services.escrow_service import (
    check_both_sides_paid,
    get_deposit_charge_id,
    lock_funds,
    refund_funds,
)
from utils.formatters import format_payment_confirmation
from utils.keyboards import back_to_menu_keyboard

logger = logging.getLogger(__name__)

# Payload prefixes stored in invoice payload to route events correctly
_CREATOR_PREFIX = "creator_"
_ACCEPTOR_PREFIX = "acceptor_"


# ---------------------------------------------------------------------------
# Invoice senders
# ---------------------------------------------------------------------------


async def _send_stars_invoice(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    challenge_uuid: str,
    amount_stars: int,
    role: str,  # "creator" or "acceptor"
    match_label: str,
    side_label: str,
) -> None:
    """
    Send a Telegram Stars payment invoice to a user for their challenge stake.

    Args:
        context:        PTB context (for context.bot).
        chat_id:        Telegram chat_id to send the invoice to.
        challenge_uuid: The challenge UUID (stored in invoice payload).
        amount_stars:   Number of Stars to charge (1 Star = 1 unit in XTR).
        role:           "creator" or "acceptor" — used to route successful_payment.
        match_label:    Human-readable match name for the invoice title.
        side_label:     Human-readable side backing for the invoice description.
    """
    # Telegram Stars payload format: "<role>_<uuid>"
    payload = f"{role}_{challenge_uuid}"

    await context.bot.send_invoice(
        chat_id=chat_id,
        title=f"⭐ SideUp Challenge Stake",
        description=(
            f"{match_label}\n"
            f"Your side: {side_label}\n"
            f"Stake: {amount_stars:,} Stars · Winner takes all"
        ),
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label="Challenge Stake", amount=amount_stars)],
        # Note: provider_token is empty string for Stars (no Stripe/etc)
        provider_token="",
    )

    logger.info(
        "Sent Stars invoice: %d XTR to chat %d for challenge %s (role=%s)",
        amount_stars,
        chat_id,
        challenge_uuid,
        role,
    )


# ---------------------------------------------------------------------------
# Callback: pay_creator_<uuid>
# ---------------------------------------------------------------------------


async def cb_pay_creator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Send a Stars invoice to the challenge creator so they can lock their stake.

    Triggered when creator taps "Pay My Stake Now" after creating a challenge.
    Pattern: "pay_creator_<uuid>"
    """
    query = update.callback_query
    await query.answer()

    challenge_uuid = query.data.replace("pay_creator_", "")
    tg_user = update.effective_user

    if tg_user is None:
        await query.answer("Could not identify your account.", show_alert=True)
        return

    async with get_db() as session:
        challenge = await challenge_service.get_challenge_by_uuid(session, challenge_uuid)

    if challenge is None:
        await query.edit_message_text(
            "❌ Challenge not found.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    if challenge.status not in ("open",):
        await query.answer(
            f"This challenge is {challenge.status}. Cannot pay now.", show_alert=True
        )
        return

    # Verify caller is the creator
    if challenge.creator and challenge.creator.telegram_id != tg_user.id:
        await query.answer(
            "Only the challenge creator can pay the creator stake.", show_alert=True
        )
        return

    match = challenge.match
    home = match.home_team
    away = match.away_team
    match_label = f"{home} vs {away}"
    side_label = {"home": f"🏠 {home}", "away": f"✈️ {away}", "draw": "🤝 Draw"}.get(
        challenge.creator_side, challenge.creator_side
    )

    await _send_stars_invoice(
        context=context,
        chat_id=tg_user.id,
        challenge_uuid=challenge_uuid,
        amount_stars=challenge.amount_stars,
        role="creator",
        match_label=match_label,
        side_label=side_label,
    )

    await query.edit_message_text(
        "⭐ <b>Stars invoice sent to your chat!</b>\n\n"
        "Check your Telegram payment prompt and confirm your stake.\n\n"
        "<i>Your Stars will be held in escrow until the match result is confirmed.</i>",
        reply_markup=back_to_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Callback: pay_acceptor_<uuid>
# ---------------------------------------------------------------------------


async def cb_pay_acceptor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Send a Stars invoice to the challenge acceptor.

    Triggered when an acceptor has been recorded and now needs to pay.
    Pattern: "pay_acceptor_<uuid>"
    """
    query = update.callback_query
    await query.answer()

    challenge_uuid = query.data.replace("pay_acceptor_", "")
    tg_user = update.effective_user

    if tg_user is None:
        await query.answer("Could not identify your account.", show_alert=True)
        return

    async with get_db() as session:
        challenge = await challenge_service.get_challenge_by_uuid(session, challenge_uuid)

    if challenge is None:
        await query.edit_message_text(
            "❌ Challenge not found.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    # Challenge must have an acceptor recorded (from market handler accept flow)
    if challenge.acceptor is None:
        await query.answer("No acceptor recorded yet.", show_alert=True)
        return

    if challenge.creator and challenge.acceptor.telegram_id != tg_user.id:
        await query.answer(
            "Only the acceptor can pay the acceptor stake.", show_alert=True
        )
        return

    match = challenge.match
    home = match.home_team
    away = match.away_team
    match_label = f"{home} vs {away}"
    acceptor_side = challenge.acceptor_side or "against_draw"
    side_label = {
        "home": f"🏠 {home}",
        "away": f"✈️ {away}",
        "against_draw": "🤝 Either team wins",
    }.get(acceptor_side, acceptor_side)

    await _send_stars_invoice(
        context=context,
        chat_id=tg_user.id,
        challenge_uuid=challenge_uuid,
        amount_stars=challenge.amount_stars,
        role="acceptor",
        match_label=match_label,
        side_label=side_label,
    )

    await query.edit_message_text(
        "⭐ <b>Stars invoice sent to your chat!</b>\n\n"
        "Confirm your stake — once both sides have paid, the challenge locks!",
        reply_markup=back_to_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Pre-checkout query — validation gate before Telegram charges the user
# ---------------------------------------------------------------------------


async def pre_checkout_query_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Validate the payment before Telegram actually charges the user.

    Telegram sends this event after the user taps "Pay" but before the
    money is deducted. We must respond within 10 seconds.

    Checks:
        - Payload is well-formed
        - Challenge exists and is still open
        - User is not the creator accepting their own challenge

    Telegram docs: https://core.telegram.org/bots/api#answerprecheckoutquery
    """
    query = update.pre_checkout_query
    payload = query.invoice_payload

    # Parse payload: "creator_<uuid>" or "acceptor_<uuid>"
    try:
        if payload.startswith(_CREATOR_PREFIX):
            role = "creator"
            challenge_uuid = payload[len(_CREATOR_PREFIX):]
        elif payload.startswith(_ACCEPTOR_PREFIX):
            role = "acceptor"
            challenge_uuid = payload[len(_ACCEPTOR_PREFIX):]
        else:
            raise ValueError(f"Unrecognised payload format: {payload}")
    except ValueError as e:
        logger.warning("Pre-checkout payload parse error: %s", e)
        await query.answer(ok=False, error_message="Invalid payment payload. Please restart.")
        return

    async with get_db() as session:
        challenge = await challenge_service.get_challenge_by_uuid(session, challenge_uuid)

    if challenge is None:
        await query.answer(ok=False, error_message="Challenge not found. It may have been deleted.")
        return

    if challenge.status not in ("open",):
        await query.answer(
            ok=False,
            error_message=(
                f"This challenge is {challenge.status}. "
                "It may have already been locked or cancelled."
            ),
        )
        return

    # Prevent creator from paying acceptor slot and vice versa
    tg_user_id = query.from_user.id
    if role == "acceptor" and challenge.creator and challenge.creator.telegram_id == tg_user_id:
        await query.answer(ok=False, error_message="You cannot accept your own challenge!")
        return

    # All good — tell Telegram to proceed with the charge
    await query.answer(ok=True)
    logger.info(
        "Pre-checkout approved: %s role=%s challenge=%s amount=%d XTR",
        tg_user_id,
        role,
        challenge_uuid,
        query.total_amount,
    )


# ---------------------------------------------------------------------------
# Successful payment — lock Stars and notify both parties
# ---------------------------------------------------------------------------


async def successful_payment_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Process a confirmed Telegram Stars payment.

    Called AFTER Telegram has charged the user successfully.

    Steps:
        1. Parse the payload to determine role (creator/acceptor) and challenge UUID
        2. Record deposit in the transactions table (via escrow_service.lock_funds)
        3. Check if both sides have paid
           - If yes: move challenge to 'locked' status, notify both parties
           - If no: tell the paying user to wait for the other side
    """
    message = update.message
    payment = message.successful_payment
    tg_user = update.effective_user

    if payment is None or tg_user is None:
        return

    payload = payment.invoice_payload
    telegram_charge_id = payment.telegram_payment_charge_id
    stars_paid = payment.total_amount  # Already in Stars units

    logger.info(
        "Successful payment: user %d paid %d XTR (charge %s) payload=%s",
        tg_user.id,
        stars_paid,
        telegram_charge_id,
        payload,
    )

    # Parse role and UUID from payload
    if payload.startswith(_CREATOR_PREFIX):
        role = "creator"
        challenge_uuid = payload[len(_CREATOR_PREFIX):]
    elif payload.startswith(_ACCEPTOR_PREFIX):
        role = "acceptor"
        challenge_uuid = payload[len(_ACCEPTOR_PREFIX):]
    else:
        logger.error("Unrecognised payment payload after charge: %s", payload)
        await message.reply_text(
            "⚠️ Payment recorded but payload unrecognised. "
            "Please contact support with your payment reference: "
            f"<code>{telegram_charge_id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    async with get_db() as session:
        challenge = await challenge_service.get_challenge_by_uuid(session, challenge_uuid)

        if challenge is None:
            logger.error(
                "Payment received for unknown challenge: %s (charge %s)",
                challenge_uuid,
                telegram_charge_id,
            )
            await message.reply_text(
                "⚠️ Payment received but challenge not found. "
                "Please contact support with reference: "
                f"<code>{telegram_charge_id}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        # Determine which user DB record corresponds to this payer
        if role == "creator":
            payer_user = challenge.creator
        else:
            payer_user = challenge.acceptor

        if payer_user is None:
            logger.error(
                "Payment for challenge %s (role=%s) but user not set.",
                challenge_uuid,
                role,
            )
            await message.reply_text(
                "⚠️ Payment received but your account record is missing. "
                "Please contact support.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Record the deposit
        await lock_funds(
            session=session,
            challenge_id=challenge.id,
            user_id=payer_user.id,
            payment_charge_id=telegram_charge_id,
            amount_stars=stars_paid,
        )

        # Check if both sides have now paid
        both_paid = await check_both_sides_paid(session, challenge)

        if both_paid:
            # Move challenge to locked status
            challenge.status = "locked"
            match = challenge.match

            # Compose confirmation messages for both participants
            creator_confirmation = format_payment_confirmation(
                challenge=challenge,
                match=match,
                user_display_name=challenge.creator.display_name() if challenge.creator else "",
                is_creator=True,
            )
            acceptor_confirmation = format_payment_confirmation(
                challenge=challenge,
                match=match,
                user_display_name=challenge.acceptor.display_name() if challenge.acceptor else "",
                is_creator=False,
            )

            # We commit before sending notifications
            await session.commit()

            # Notify creator
            if challenge.creator:
                try:
                    await context.bot.send_message(
                        chat_id=challenge.creator.telegram_id,
                        text=creator_confirmation,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.warning(
                        "Could not notify creator %d: %s",
                        challenge.creator.telegram_id,
                        e,
                    )

            # Notify acceptor
            if challenge.acceptor:
                try:
                    await context.bot.send_message(
                        chat_id=challenge.acceptor.telegram_id,
                        text=acceptor_confirmation,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.warning(
                        "Could not notify acceptor %d: %s",
                        challenge.acceptor.telegram_id,
                        e,
                    )

            # Confirmation to the current payer (in case they need reassurance)
            await message.reply_text(
                "🔒 <b>Both sides locked! Challenge is ON.</b>\n\n"
                "I'll check the result automatically after the match ends. "
                "Good luck! 🤞",
                parse_mode=ParseMode.HTML,
            )
        else:
            # Only one side has paid; tell them to wait
            await message.reply_text(
                f"✅ <b>Your {stars_paid:,} Stars are locked in!</b>\n\n"
                "Waiting for the other side to pay their stake. "
                "Once they do, the challenge will be locked and I'll notify you both.",
                parse_mode=ParseMode.HTML,
            )


# ---------------------------------------------------------------------------
# Refund utility (called from admin and scheduler)
# ---------------------------------------------------------------------------


async def issue_refund(
    context: ContextTypes.DEFAULT_TYPE,
    user_telegram_id: int,
    telegram_payment_charge_id: str,
) -> bool:
    """
    Issue a Telegram Stars refund for a specific payment charge.

    Uses the Telegram refundStarPayment API endpoint, which requires the
    original telegram_payment_charge_id from the successful_payment event.

    Args:
        context:                      PTB context (for context.bot).
        user_telegram_id:             Telegram user_id of the recipient.
        telegram_payment_charge_id:   Original charge ID to refund.

    Returns:
        True if the refund was issued successfully, False on error.

    Note:
        Telegram only allows refunds for charges that have not yet been used
        (i.e., not withdrawn). This is always valid for unexpired challenges.
    """
    try:
        await context.bot.refund_star_payment(
            user_id=user_telegram_id,
            telegram_payment_charge_id=telegram_payment_charge_id,
        )
        logger.info(
            "Refund issued: user %d charge %s",
            user_telegram_id,
            telegram_payment_charge_id,
        )
        return True
    except Exception as e:
        logger.error(
            "Refund failed for user %d charge %s: %s",
            user_telegram_id,
            telegram_payment_charge_id,
            e,
        )
        return False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app: Application) -> None:
    """
    Register payment handlers with the Application.

    Args:
        app: The PTB Application instance.
    """
    # Invoice trigger callbacks (from challenge creation and market accept flows)
    app.add_handler(CallbackQueryHandler(cb_pay_creator, pattern="^pay_creator_"))
    app.add_handler(CallbackQueryHandler(cb_pay_acceptor, pattern="^pay_acceptor_"))

    # Telegram payment lifecycle
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_query_handler))
    app.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler)
    )

    logger.info("Payment handlers registered.")
