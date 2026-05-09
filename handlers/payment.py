"""
handlers/payment.py — Telegram Stars payment flow (pre-checkout + successful_payment).
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from db import get_db
from services.challenge_service import (
    get_challenge_by_uuid,
    lock_stars_acceptor,
    lock_stars_creator,
)

logger = logging.getLogger(__name__)


async def pre_checkout_query_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Validate the invoice before Telegram charges the user.
    Must answer within 10 seconds.
    """
    pq = update.pre_checkout_query
    payload = pq.invoice_payload  # e.g. "creator_{uuid}" or "acceptor_{uuid}"

    try:
        role, uuid = payload.split("_", 1)
    except ValueError:
        await pq.answer(ok=False, error_message="Invalid payment payload.")
        return

    async with get_db() as session:
        challenge = await get_challenge_by_uuid(session, uuid)

    if not challenge:
        await pq.answer(ok=False, error_message="This challenge no longer exists.")
        return

    if challenge.status != "open":
        await pq.answer(
            ok=False,
            error_message="This challenge is no longer open. Your Stars won't be charged.",
        )
        return

    if role == "acceptor" and pq.from_user.id == challenge.creator.telegram_id:
        await pq.answer(ok=False, error_message="You can't accept your own challenge.")
        return

    # All good — allow the payment
    await pq.answer(ok=True)


async def successful_payment_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Called after Telegram successfully charges Stars.
    Update the challenge and notify users.
    """
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id
    user = update.effective_user

    try:
        role, uuid = payload.split("_", 1)
    except ValueError:
        logger.error("Bad payment payload: %s", payload)
        return

    try:
        async with get_db() as session:
            if role == "creator":
                challenge = await lock_stars_creator(session, uuid, charge_id)
                invite_link = challenge.invite_link

                # Send invite link to creator
                await update.message.reply_text(
                    f"🔒 *Stars locked!*\n\n"
                    f"Your challenge is live. Share this link with your friend:\n\n"
                    f"`{invite_link}`\n\n"
                    f"_(tap to copy)_\n\n"
                    f"Once they accept and lock their Stars, the challenge is on! "
                    f"Bot will auto-resolve after the match.",
                    parse_mode="Markdown",
                )

            elif role == "acceptor":
                challenge = await lock_stars_acceptor(
                    session,
                    uuid,
                    acceptor_telegram_id=user.id,
                    acceptor_first_name=user.first_name,
                    acceptor_username=user.username,
                    telegram_charge_id=charge_id,
                )

                match = challenge.match
                creator = challenge.creator

                # Notify acceptor
                await update.message.reply_text(
                    f"🔒 *You're in!*\n\n"
                    f"Challenge locked — both sides have paid.\n"
                    f"🏟️ {match.home_team} vs {match.away_team}\n"
                    f"Your side: *{challenge.acceptor_side.upper()}*\n"
                    f"Pot: *{challenge.pot_stars} ⭐ Stars*\n\n"
                    f"The bot will automatically pay the winner after the match. Good luck! 🍀",
                    parse_mode="Markdown",
                )

                # Notify creator
                acceptor_name = f"@{user.username}" if user.username else user.first_name
                try:
                    await context.bot.send_message(
                        chat_id=creator.telegram_id,
                        text=(
                            f"⚡ *{acceptor_name} accepted your challenge!*\n\n"
                            f"🏟️ {match.home_team} vs {match.away_team}\n"
                            f"Your side: *{challenge.creator_side.upper()}*\n"
                            f"Pot: *{challenge.pot_stars} ⭐ Stars*\n\n"
                            f"Challenge is LOCKED. Winner auto-paid after the match. 🍀"
                        ),
                        parse_mode="Markdown",
                    )
                except Exception as exc:
                    logger.warning("Could not notify creator %s: %s", creator.telegram_id, exc)

    except ValueError as exc:
        logger.error("Payment processing error for payload %s: %s", payload, exc)
        await update.message.reply_text(
            "⚠️ There was an issue processing your payment. "
            "Please contact @sideupbot support with your payment ID: "
            f"`{charge_id}`",
            parse_mode="Markdown",
        )
