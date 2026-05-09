"""
handlers/admin.py — Admin-only commands for manual challenge management.
Only users in ADMIN_USER_IDS can use these.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_USER_IDS
from db import get_db
from services.challenge_service import get_admin_stats, get_challenge_by_uuid, resolve_challenge

logger = logging.getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/admin — show platform stats."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    async with get_db() as session:
        stats = await get_admin_stats(session)

    await update.message.reply_text(
        f"🛠️ *SideUp Admin Dashboard*\n\n"
        f"Total challenges: {stats['total']}\n"
        f"Active (open + locked): {stats['active']}\n"
        f"Resolved: {stats['resolved']}\n"
        f"⭐ Stars in escrow: {stats['escrow_stars']}\n\n"
        f"Commands:\n"
        f"/resolve `<uuid>` `<home|draw|away>` — manually resolve\n"
        f"/refundall `<uuid>` — cancel and refund both sides",
        parse_mode="Markdown",
    )


async def resolve_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/resolve <uuid> <home|draw|away> — manually set the winner."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /resolve <challenge_uuid> <home|draw|away>")
        return

    uuid, winning_side = args[0], args[1].lower()
    if winning_side not in ("home", "draw", "away"):
        await update.message.reply_text("winning_side must be: home, draw, or away")
        return

    try:
        async with get_db() as session:
            challenge, winner, payout = await resolve_challenge(session, uuid, winning_side)

        await update.message.reply_text(
            f"✅ Challenge `{uuid[:8]}…` resolved.\n"
            f"Winner: user_id={challenge.winner_id}\n"
            f"Payout: {payout} ⭐\n\n"
            f"_(Remember to manually refund Stars via BotFather refund API if needed)_",
            parse_mode="Markdown",
        )

        # Notify winner via bot
        try:
            await context.bot.send_message(
                chat_id=winner.telegram_id,
                text=(
                    f"🏆 *Admin resolved your challenge — you won!*\n"
                    f"Payout: *{payout} ⭐ Stars*\n"
                    f"_(Manual resolution by admin)_"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    except ValueError as exc:
        await update.message.reply_text(f"❌ Error: {exc}")
    except Exception as exc:
        logger.error("Admin resolve error: %s", exc)
        await update.message.reply_text(f"❌ Unexpected error: {exc}")


async def refund_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/refundall <uuid> — cancel a challenge and mark it for refund."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /refundall <challenge_uuid>")
        return

    uuid = args[0]
    async with get_db() as session:
        challenge = await get_challenge_by_uuid(session, uuid)
        if not challenge:
            await update.message.reply_text(f"❌ Challenge {uuid} not found.")
            return

        challenge.status = "cancelled"
        await session.flush()

    await update.message.reply_text(
        f"✅ Challenge `{uuid[:8]}…` cancelled.\n\n"
        f"⚠️ *Action required:* Refund Stars manually using the Telegram Bot API:\n"
        f"`POST /refundStarPayment` for charge IDs stored in the transactions table.",
        parse_mode="Markdown",
    )

    # Notify both parties
    try:
        await context.bot.send_message(
            chat_id=challenge.creator.telegram_id,
            text=f"⚠️ Your challenge `{uuid[:8]}…` was cancelled by admin. Refund incoming.",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    if challenge.acceptor:
        try:
            await context.bot.send_message(
                chat_id=challenge.acceptor.telegram_id,
                text=f"⚠️ Challenge `{uuid[:8]}…` was cancelled by admin. Refund incoming.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
