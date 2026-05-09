"""
services/scheduler.py — SideUp Bot APScheduler Jobs
=====================================================
Background jobs that run on a timer to automate challenge resolution
and housekeeping. Uses APScheduler's AsyncScheduler (asyncio-native).

Jobs:
    poll_match_results()   — Every 5 minutes. Finds locked challenges whose
                             match has kicked off, checks results via sports APIs,
                             auto-resolves finished matches and notifies users.

    expire_challenges()    — Every hour. Marks unaccepted open challenges as
                             expired after CHALLENGE_EXPIRY_HOURS.

The bot Application instance is injected at startup via set_application()
so jobs can send Telegram notifications.

Usage in main.py:
    from services.scheduler import create_scheduler, set_application
    set_application(app)
    scheduler = create_scheduler()
    scheduler.start()
    ...
    scheduler.shutdown()
"""

import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application

from config import RESULT_CHECK_INTERVAL_MINUTES
from db import get_db
from services import challenge_service
from services.escrow_service import get_deposit_charge_id, refund_funds, release_funds
from services import sports_api as sports_api_service
from utils.formatters import format_payment_confirmation

logger = logging.getLogger(__name__)

# Application instance injected at startup — used to send messages
_application: Optional[Application] = None


def set_application(app: Application) -> None:
    """
    Inject the PTB Application so scheduler jobs can send Telegram messages.

    Must be called before starting the scheduler.

    Args:
        app: The running PTB Application instance.
    """
    global _application
    _application = app
    logger.info("Scheduler application instance set.")


def _get_bot() -> Optional[Bot]:
    """Return the Bot from the injected Application, or None."""
    if _application is not None:
        return _application.bot
    return None


# ---------------------------------------------------------------------------
# Job: poll_match_results
# ---------------------------------------------------------------------------


async def poll_match_results() -> None:
    """
    APScheduler job — runs every RESULT_CHECK_INTERVAL_MINUTES minutes.

    For each locked challenge whose match kickoff time has passed:
        1. Query the sports API for the match result
        2. If finished: resolve the challenge, record payout, notify both users
        3. If not finished: skip (will be checked again next cycle)

    Safe to run concurrently with bot handlers — each call opens its own
    database session and commits independently.
    """
    logger.info("Starting poll_match_results job...")
    resolved = 0
    errors = 0

    bot = _get_bot()
    if bot is None:
        logger.warning("poll_match_results: no bot instance — skipping notification step.")

    async with get_db() as session:
        pending = await challenge_service.get_locked_challenges_for_resolution(session)
        logger.info("Found %d locked challenges ready to check.", len(pending))

        for challenge in pending:
            match = challenge.match
            try:
                # Check if match has a result
                result = await sports_api_service.get_match_result(
                    external_id=match.external_id,
                    league=match.league,
                )

                if result is None:
                    logger.debug(
                        "Match %s (%s vs %s) not yet finished.",
                        match.external_id,
                        match.home_team,
                        match.away_team,
                    )
                    continue

                # Update match record with the result
                match.status = result["status"]
                match.home_score = result.get("home_score")
                match.away_score = result.get("away_score")
                match.winner = result["winner"]

                # Resolve the challenge
                winner, payout_stars, fee_stars = await challenge_service.resolve_challenge(
                    session=session,
                    challenge=challenge,
                    winner_side=result["winner"],
                )

                # Record payout in escrow ledger
                await release_funds(
                    session=session,
                    challenge=challenge,
                    winner=winner,
                    payout_amount=payout_stars,
                    fee_amount=fee_stars,
                )

                resolved += 1
                logger.info(
                    "Auto-resolved challenge %s: %s %s–%s %s → winner %s (%d ⭐)",
                    challenge.uuid,
                    match.home_team,
                    match.home_score,
                    match.away_score,
                    match.away_team,
                    winner.display_name(),
                    payout_stars,
                )

                # Commit before sending notifications
                await session.commit()

                # Send winner/loser notifications
                if bot is not None:
                    await _notify_resolution(bot, challenge, match, winner, payout_stars)

            except Exception as exc:
                errors += 1
                logger.error(
                    "Error resolving challenge %s: %s",
                    challenge.uuid,
                    exc,
                    exc_info=True,
                )
                # Continue processing remaining challenges — don't abort the batch

    logger.info(
        "poll_match_results complete: %d resolved, %d errors.",
        resolved,
        errors,
    )


async def _notify_resolution(
    bot: Bot,
    challenge,
    match,
    winner,
    payout_stars: int,
) -> None:
    """
    Send result notifications to both the winner and loser.

    Args:
        bot:          The Telegram Bot instance.
        challenge:    The resolved Challenge.
        match:        The Match with final scores.
        winner:       The winning User.
        payout_stars: Stars awarded to the winner.
    """
    home_score = match.home_score if match.home_score is not None else "?"
    away_score = match.away_score if match.away_score is not None else "?"
    score_line = f"{home_score} – {away_score}"

    loser = (
        challenge.acceptor
        if challenge.winner_id == challenge.creator_id
        else challenge.creator
    )

    winner_msg = (
        f"🏆 <b>You won!</b>\n\n"
        f"<b>{match.home_team} vs {match.away_team}</b>\n"
        f"Final score: {score_line}\n\n"
        f"Your payout: <b>{payout_stars:,} ⭐ Stars</b>\n\n"
        f"⚠️ <i>Our team will process your Stars payout shortly. "
        f"Check your Telegram wallet.</i>\n\n"
        f"<i>Challenge: <code>{challenge.uuid[:8]}</code></i>"
    )

    loser_msg = (
        f"💔 <b>Better luck next time.</b>\n\n"
        f"<b>{match.home_team} vs {match.away_team}</b>\n"
        f"Final score: {score_line}\n\n"
        f"{winner.display_name()} called it right. "
        f"Challenge: <code>{challenge.uuid[:8]}</code>\n\n"
        f"Ready for a rematch? Create a new challenge 🔥"
    )

    try:
        await bot.send_message(
            chat_id=winner.telegram_id,
            text=winner_msg,
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.warning(
            "Could not notify winner %d: %s", winner.telegram_id, exc
        )

    if loser:
        try:
            await bot.send_message(
                chat_id=loser.telegram_id,
                text=loser_msg,
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning(
                "Could not notify loser %d: %s", loser.telegram_id, exc
            )


# ---------------------------------------------------------------------------
# Job: expire_challenges
# ---------------------------------------------------------------------------


async def expire_challenges() -> None:
    """
    APScheduler job — runs every hour.

    Marks open challenges older than CHALLENGE_EXPIRY_HOURS as 'expired'.

    Note: For 'open' challenges, no Stars have been deposited yet
    (payment only happens after acceptance), so no refund is needed.
    The job just cleans up stale open challenges.

    However, if a challenge is in an intermediate state where the acceptor
    has paid but the creator hasn't (edge case), the locked escrow is
    recorded in the transactions table — admin can manually refund via /refundall.
    """
    logger.info("Starting expire_challenges job...")

    async with get_db() as session:
        count = await challenge_service.expire_old_challenges(session)

    logger.info("expire_challenges complete: %d challenges expired.", count)


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------


def create_scheduler() -> AsyncIOScheduler:
    """
    Create and configure the APScheduler AsyncIOScheduler with both jobs.

    Jobs:
        poll_match_results  — every RESULT_CHECK_INTERVAL_MINUTES (default: 15 → 5min)
        expire_challenges   — every 60 minutes

    Returns:
        Configured (but not yet started) AsyncIOScheduler instance.

    Usage:
        scheduler = create_scheduler()
        scheduler.start()
        ...
        scheduler.shutdown(wait=False)
    """
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        poll_match_results,
        trigger="interval",
        minutes=RESULT_CHECK_INTERVAL_MINUTES,
        id="poll_match_results",
        name="Poll match results and auto-resolve challenges",
        replace_existing=True,
        max_instances=1,          # Prevent overlapping runs
        misfire_grace_time=60,    # Allow up to 60s late start
    )

    scheduler.add_job(
        expire_challenges,
        trigger="interval",
        hours=1,
        id="expire_challenges",
        name="Expire stale open challenges",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    logger.info(
        "Scheduler configured: poll_match_results every %dmin, expire_challenges every 1h.",
        RESULT_CHECK_INTERVAL_MINUTES,
    )
    return scheduler
