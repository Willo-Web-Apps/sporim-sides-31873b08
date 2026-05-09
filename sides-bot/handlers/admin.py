"""
handlers/admin.py — SideUp Bot Admin Commands
===============================================
Admin-only commands for platform management. All handlers verify
the caller is in ADMIN_USER_IDS before executing.

Commands:
    /admin              — Platform statistics dashboard
    /resolve <uuid> <home|draw|away>   — Manually resolve a challenge
    /refundall <uuid>   — Refund both sides and cancel a challenge
    /pending            — List locked challenges awaiting resolution

All operations are logged for audit purposes. Admin Telegram IDs are
set via ADMIN_USER_IDS in the environment (.env or Railway variables).
"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from config import ADMIN_USER_IDS
from db import get_db
from services import challenge_service
from services.escrow_service import (
    get_deposit_charge_id,
    get_escrow_stats,
    refund_funds,
    release_funds,
)
from utils.formatters import (
    format_pending_challenges,
    format_stats,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


def _is_admin(update: Update) -> bool:
    """
    Return True if the effective user is in ADMIN_USER_IDS.

    Args:
        update: Incoming Telegram update.

    Returns:
        True if admin, False otherwise.
    """
    user = update.effective_user
    if user is None:
        return False
    return user.id in ADMIN_USER_IDS


async def _deny(update: Update) -> None:
    """Send a generic denial message to non-admins."""
    await update.message.reply_text(
        "⛔ This command is restricted to bot administrators.",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# /admin — stats dashboard
# ---------------------------------------------------------------------------


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Display the platform statistics dashboard.

    Shows:
        - Total users
        - Challenge counts by status (open, locked, resolved, cancelled)
        - Total Stars volume in escrow
        - Total platform fees collected

    Only accessible to users listed in ADMIN_USER_IDS.
    """
    if not _is_admin(update):
        await _deny(update)
        return

    async with get_db() as session:
        stats = await challenge_service.get_platform_stats(session)

    text = format_stats(stats)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    logger.info("Admin stats requested by user %d", update.effective_user.id)


# ---------------------------------------------------------------------------
# /pending — list locked challenges
# ---------------------------------------------------------------------------


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    List all locked challenges that are waiting for a match result.

    Useful for monitoring which challenges need auto- or manual resolution.

    Only accessible to users listed in ADMIN_USER_IDS.
    """
    if not _is_admin(update):
        await _deny(update)
        return

    async with get_db() as session:
        challenges = await challenge_service.get_locked_challenges_for_resolution(session)

    text = format_pending_challenges(challenges)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    logger.info(
        "Admin pending challenges requested by user %d — %d locked",
        update.effective_user.id,
        len(challenges),
    )


# ---------------------------------------------------------------------------
# /resolve <uuid> <home|draw|away>
# ---------------------------------------------------------------------------


async def resolve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Manually resolve a challenge by setting the winning side.

    Usage:
        /resolve <challenge_uuid> <home|draw|away>

    Example:
        /resolve a3f8c2d1e4b0 home

    Steps:
        1. Load and validate the challenge (must be 'locked')
        2. Call challenge_service.resolve_challenge() to determine winner
        3. Record payout in escrow via release_funds()
        4. Notify both participants via Telegram DM

    The actual Stars transfer must be executed manually (or via Telegram
    refundStarPayment to the winner once Telegram supports bot-to-user
    Star transfers). This command records intent and notifies users.

    Only accessible to users listed in ADMIN_USER_IDS.
    """
    if not _is_admin(update):
        await _deny(update)
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b> <code>/resolve &lt;uuid&gt; &lt;home|draw|away&gt;</code>\n\n"
            "Example: <code>/resolve a3f8c2d1 home</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    challenge_uuid = args[0]
    winning_side = args[1].lower()

    if winning_side not in ("home", "draw", "away"):
        await update.message.reply_text(
            "⚠️ Winning side must be one of: <b>home</b>, <b>draw</b>, <b>away</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    async with get_db() as session:
        challenge = await challenge_service.get_challenge_by_uuid(session, challenge_uuid)

        if challenge is None:
            await update.message.reply_text(
                f"❌ Challenge <code>{challenge_uuid}</code> not found.",
                parse_mode=ParseMode.HTML,
            )
            return

        if challenge.status != "locked":
            await update.message.reply_text(
                f"⚠️ Challenge status is <b>{challenge.status}</b>. "
                "Only <b>locked</b> challenges can be resolved.",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            winner, payout_stars, fee_stars = await challenge_service.resolve_challenge(
                session=session,
                challenge=challenge,
                winner_side=winning_side,
            )
        except ValueError as e:
            await update.message.reply_text(
                f"❌ Resolution error: {e}",
                parse_mode=ParseMode.HTML,
            )
            return

        # Record the payout in the escrow ledger
        await release_funds(
            session=session,
            challenge=challenge,
            winner=winner,
            payout_amount=payout_stars,
            fee_amount=fee_stars,
        )

        match = challenge.match
        winner_name = winner.display_name()
        loser = (
            challenge.acceptor
            if challenge.winner_id == challenge.creator_id
            else challenge.creator
        )

    # Admin confirmation
    admin_msg = (
        f"✅ <b>Challenge Resolved</b>\n\n"
        f"UUID: <code>{challenge_uuid}</code>\n"
        f"Match: {match.home_team} vs {match.away_team}\n"
        f"Winning side: <b>{winning_side}</b>\n\n"
        f"🏆 Winner: {winner_name}\n"
        f"💰 Payout: {payout_stars:,} ⭐\n"
        f"💼 Platform fee: {fee_stars:,} ⭐\n\n"
        f"⚠️ <i>Remember to manually transfer {payout_stars:,} Stars "
        f"to {winner_name}. Use Telegram's refund API with the winner's "
        f"deposit charge ID to send Stars.</i>"
    )
    await update.message.reply_text(admin_msg, parse_mode=ParseMode.HTML)

    # Notify winner
    winner_msg = (
        f"🏆 <b>You won!</b>\n\n"
        f"<b>{match.home_team} vs {match.away_team}</b>\n"
        f"Result: {winning_side.upper()}\n\n"
        f"Payout: <b>{payout_stars:,} ⭐ Stars</b>\n\n"
        f"⚠️ <i>Our team will process your Stars payout shortly. "
        f"You'll see them in your Telegram wallet. "
        f"Challenge ID: <code>{challenge.uuid[:8]}</code></i>"
    )
    try:
        await context.bot.send_message(
            chat_id=winner.telegram_id,
            text=winner_msg,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("Could not notify winner %d: %s", winner.telegram_id, e)

    # Notify loser
    if loser:
        loser_msg = (
            f"💔 <b>Better luck next time.</b>\n\n"
            f"<b>{match.home_team} vs {match.away_team}</b>\n"
            f"Result: {winning_side.upper()}\n\n"
            f"{winner_name} called it right. "
            f"Challenge ID: <code>{challenge.uuid[:8]}</code>\n\n"
            f"Ready for a rematch? 🔥"
        )
        try:
            await context.bot.send_message(
                chat_id=loser.telegram_id,
                text=loser_msg,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("Could not notify loser %d: %s", loser.telegram_id, e)

    logger.info(
        "Admin %d resolved challenge %s: winner %d, payout %d ⭐",
        update.effective_user.id,
        challenge_uuid,
        winner.id,
        payout_stars,
    )


# ---------------------------------------------------------------------------
# /refundall <uuid>
# ---------------------------------------------------------------------------


async def refundall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Refund both sides of a challenge and mark it as cancelled.

    Usage:
        /refundall <challenge_uuid>

    This is used for:
        - Postponed/cancelled matches
        - Disputes requiring admin intervention
        - Challenges stuck in a bad state

    Steps:
        1. Load challenge (must be 'open' or 'locked')
        2. Fetch each participant's deposit charge ID
        3. Issue Telegram refundStarPayment for each deposit
        4. Record refund transactions in the DB
        5. Set challenge status to 'cancelled'
        6. Notify both parties

    Only accessible to users listed in ADMIN_USER_IDS.
    """
    if not _is_admin(update):
        await _deny(update)
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b> <code>/refundall &lt;uuid&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    challenge_uuid = args[0]

    async with get_db() as session:
        challenge = await challenge_service.get_challenge_by_uuid(session, challenge_uuid)

        if challenge is None:
            await update.message.reply_text(
                f"❌ Challenge <code>{challenge_uuid}</code> not found.",
                parse_mode=ParseMode.HTML,
            )
            return

        if challenge.status not in ("open", "locked"):
            await update.message.reply_text(
                f"⚠️ Challenge status is <b>{challenge.status}</b>. "
                "Only <b>open</b> or <b>locked</b> challenges can be refunded.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Collect participants and their charge IDs
        participants = []
        if challenge.creator:
            creator_charge = await get_deposit_charge_id(
                session, challenge.id, challenge.creator.id
            )
            participants.append((challenge.creator, creator_charge))

        if challenge.acceptor:
            acceptor_charge = await get_deposit_charge_id(
                session, challenge.id, challenge.acceptor.id
            )
            participants.append((challenge.acceptor, acceptor_charge))

        refund_results = []
        for participant, charge_id in participants:
            # Record refund in DB
            await refund_funds(
                session=session,
                challenge=challenge,
                user=participant,
                amount_stars=challenge.amount_stars,
            )

            # Issue Telegram refund if we have a charge ID
            if charge_id:
                success = False
                try:
                    await context.bot.refund_star_payment(
                        user_id=participant.telegram_id,
                        telegram_payment_charge_id=charge_id,
                    )
                    success = True
                    logger.info(
                        "Refund issued to user %d for challenge %s (charge %s)",
                        participant.telegram_id,
                        challenge_uuid,
                        charge_id,
                    )
                except Exception as e:
                    logger.error(
                        "Refund failed for user %d charge %s: %s",
                        participant.telegram_id,
                        charge_id,
                        e,
                    )
                refund_results.append(
                    f"  • {participant.display_name()}: "
                    f"{'✅ refunded' if success else '❌ FAILED — manual action needed'}"
                )
            else:
                refund_results.append(
                    f"  • {participant.display_name()}: ⚠️ no charge ID — never paid"
                )

        # Cancel the challenge
        challenge.status = "cancelled"

        match = challenge.match

    # Admin confirmation
    refund_summary = "\n".join(refund_results) if refund_results else "  No participants had paid."
    admin_msg = (
        f"↩️ <b>Refund Complete</b>\n\n"
        f"UUID: <code>{challenge_uuid}</code>\n"
        f"Match: {match.home_team} vs {match.away_team}\n\n"
        f"Refund status:\n{refund_summary}\n\n"
        f"Challenge status set to: <b>cancelled</b>"
    )
    await update.message.reply_text(admin_msg, parse_mode=ParseMode.HTML)

    # Notify participants
    for participant, _ in participants:
        try:
            await context.bot.send_message(
                chat_id=participant.telegram_id,
                text=(
                    f"↩️ <b>Challenge Cancelled — Refund Issued</b>\n\n"
                    f"<b>{match.home_team} vs {match.away_team}</b>\n\n"
                    f"Your {challenge.amount_stars:,} ⭐ Stars have been refunded. "
                    f"You should see them in your Telegram wallet shortly.\n\n"
                    f"<i>Challenge ID: <code>{challenge.uuid[:8]}</code></i>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning(
                "Could not notify participant %d about refund: %s",
                participant.telegram_id,
                e,
            )

    logger.info(
        "Admin %d issued refundall for challenge %s",
        update.effective_user.id,
        challenge_uuid,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app: Application) -> None:
    """
    Register admin command handlers with the Application.

    Args:
        app: The PTB Application instance.
    """
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("resolve", resolve_command))
    app.add_handler(CommandHandler("refundall", refundall_command))

    logger.info(
        "Admin handlers registered. Authorised admin IDs: %s",
        ADMIN_USER_IDS or "NONE (set ADMIN_USER_IDS in .env!)",
    )
