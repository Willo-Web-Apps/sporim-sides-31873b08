"""
services/result_checker.py — SIDES Bot Automatic Result Checker
================================================================
APScheduler job that polls sports APIs for match results and
auto-resolves locked challenges.

Called every 15 minutes by the scheduler in main.py.

Flow:
    1. Find all 'locked' challenges where kickoff_time < now
    2. For each, fetch match result from sports API
    3. If match is finished, call resolve_challenge()
    4. Notify both users via Telegram
    5. Expire stale open challenges

The bot application instance is passed in at startup so we can
send Telegram messages from within this async job.
"""

import logging
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from database import get_session
from services import challenge_service, sports_api
from services.escrow_service import release_funds
from utils.formatters import format_payment_confirmation

logger = logging.getLogger(__name__)

# Will be set by main.py at startup
_bot: Optional[Bot] = None


def set_bot(bot: Bot) -> None:
    """
    Inject the Telegram bot instance so this module can send messages.
    Called once during application startup.
    """
    global _bot
    _bot = bot
    logger.info("Result checker bot instance configured.")


async def _notify_users(challenge, match, winner, payout_stars: int) -> None:
    """Send resolution notifications to both challenge participants."""
    if _bot is None:
        logger.warning("Bot not set — cannot send resolution notifications.")
        return

    winner_name = winner.display_name()
    loser = (
        challenge.acceptor
        if challenge.winner_id == challenge.creator_id
        else challenge.creator
    )

    # Message to winner
    winner_msg = (
        f"🏆 <b>You won!</b>\n\n"
        f"<b>{match.home_team} vs {match.away_team}</b>\n"
        f"Final: {match.home_score} – {match.away_score}\n\n"
        f"You backed the right side. Your payout: <b>{payout_stars:,} ⭐</b>\n\n"
        f"⚠️ <i>V1 Notice: Our team will process your Stars payout shortly. "
        f"Keep an eye on your Telegram wallet.</i>\n\n"
        f"<i>Challenge: <code>{challenge.uuid[:8]}</code></i>"
    )

    # Message to loser
    loser_msg = (
        f"💔 <b>Better luck next time.</b>\n\n"
        f"<b>{match.home_team} vs {match.away_team}</b>\n"
        f"Final: {match.home_score} – {match.away_score}\n\n"
        f"{winner_name} called it right this time.\n"
        f"Create a new challenge and get your Stars back? 🔥\n\n"
        f"<i>Challenge: <code>{challenge.uuid[:8]}</code></i>"
    )

    try:
        await _bot.send_message(
            chat_id=winner.telegram_id,
            text=winner_msg,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("Failed to notify winner %d: %s", winner.telegram_id, e)

    if loser:
        try:
            await _bot.send_message(
                chat_id=loser.telegram_id,
                text=loser_msg,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error("Failed to notify loser %d: %s", loser.telegram_id, e)


async def check_pending_results() -> None:
    """
    Main job function — called by APScheduler every 15 minutes.

    Steps:
        1. Expire stale open challenges
        2. Find locked challenges where match has kicked off
        3. Fetch results from sports APIs
        4. Resolve finished challenges and notify users
    """
    logger.info("Running result checker...")
    resolved_count = 0
    error_count = 0

    async with get_session() as session:
        # Step 1: Expire old open challenges
        expired = await challenge_service.expire_old_challenges(session)
        if expired:
            logger.info("Expired %d stale challenges", expired)

        # Step 2: Get locked challenges ready for resolution
        pending = await challenge_service.get_locked_challenges_for_resolution(session)
        logger.info("Found %d locked challenges to check", len(pending))

        for challenge in pending:
            match = challenge.match
            try:
                # Step 3: Fetch match result
                result = await sports_api.get_match_result(
                    external_id=match.external_id,
                    league=match.league,
                )

                if result is None:
                    # Match not finished yet — check again next cycle
                    logger.debug(
                        "Match %s (%s vs %s) not finished yet.",
                        match.external_id,
                        match.home_team,
                        match.away_team,
                    )
                    continue

                # Update match record
                match.status = result["status"]
                match.home_score = result.get("home_score")
                match.away_score = result.get("away_score")
                match.winner = result["winner"]

                # Step 4: Resolve the challenge
                winner, payout_stars, fee_stars = await challenge_service.resolve_challenge(
                    session=session,
                    challenge=challenge,
                    winner_side=result["winner"],
                )

                # Record payout in escrow
                await release_funds(
                    session=session,
                    challenge=challenge,
                    winner=winner,
                    payout_amount=payout_stars,
                    fee_amount=fee_stars,
                )

                resolved_count += 1
                logger.info(
                    "Auto-resolved challenge %s: %s %d–%d %s → winner %d (%d ⭐)",
                    challenge.uuid,
                    match.home_team,
                    match.home_score or 0,
                    match.away_score or 0,
                    match.away_team,
                    winner.id,
                    payout_stars,
                )

                # Commit before sending notifications (avoid holding TX open)
                await session.commit()

                # Step 5: Notify users
                await _notify_users(challenge, match, winner, payout_stars)

            except Exception as e:
                error_count += 1
                logger.error(
                    "Error processing challenge %s: %s",
                    challenge.uuid,
                    e,
                    exc_info=True,
                )
                # Don't let one bad challenge abort the whole batch
                continue

    logger.info(
        "Result checker complete: %d resolved, %d errors.",
        resolved_count,
        error_count,
    )
