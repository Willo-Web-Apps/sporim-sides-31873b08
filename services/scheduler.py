"""
services/scheduler.py — APScheduler jobs for result polling and challenge expiry.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from db import async_session_factory
from models import Challenge, Match
from services.challenge_service import expire_old_challenges, resolve_challenge
from services.sports_api import FootballDataClient, BallDontLieClient

logger = logging.getLogger(__name__)

_bot_app = None  # set at startup


def set_bot_app(app) -> None:
    """Inject the PTB Application so the scheduler can send messages."""
    global _bot_app
    _bot_app = app


async def poll_match_results() -> None:
    """
    Every 5 minutes: find locked challenges whose match has ended,
    fetch the result, resolve, and notify both users.
    """
    logger.info("Scheduler: polling match results…")
    football_client = FootballDataClient()
    nba_client = BallDontLieClient()

    async with async_session_factory() as session:
        result = await session.execute(
            select(Challenge)
            .where(Challenge.status == "locked")
            .join(Challenge.match)
        )
        locked_challenges = list(result.scalars().all())

        if not locked_challenges:
            return

        # Collect unique match IDs to check
        match_ids = {c.match_id for c in locked_challenges}
        matches_result = await session.execute(
            select(Match).where(Match.id.in_(match_ids))
        )
        matches = {m.id: m for m in matches_result.scalars().all()}

        for challenge in locked_challenges:
            match = matches.get(challenge.match_id)
            if not match or match.status == "finished":
                if match and match.status == "finished" and match.winner != "tbd":
                    await _resolve_and_notify(session, challenge, match.winner)
                continue

            # Fetch fresh result
            try:
                if match.league == "NBA":
                    result_data = await nba_client.get_game_result(match.external_id)
                else:
                    result_data = await football_client.get_match_result(
                        match.external_id, match.league
                    )

                if result_data and result_data.get("status") == "finished":
                    match.status = "finished"
                    match.home_score = result_data.get("home_score")
                    match.away_score = result_data.get("away_score")
                    match.winner = result_data.get("winner", "tbd")
                    await session.flush()

                    if match.winner != "tbd":
                        await _resolve_and_notify(session, challenge, match.winner)

            except Exception as exc:
                logger.error("Error polling result for match %s: %s", match.external_id, exc)

        await session.commit()


async def _resolve_and_notify(session, challenge: Challenge, winning_side: str) -> None:
    """Resolve a challenge and send Telegram notifications."""
    try:
        resolved, winner, payout = await resolve_challenge(session, challenge.uuid, winning_side)

        if _bot_app:
            loser_id = (
                challenge.creator.telegram_id
                if winner.id == challenge.acceptor_id
                else challenge.acceptor.telegram_id
            )
            # Notify winner
            await _bot_app.bot.send_message(
                chat_id=winner.telegram_id,
                text=(
                    f"🏆 *You won!*\n\n"
                    f"Challenge resolved — {winning_side.capitalize()} side wins!\n"
                    f"*+{payout} ⭐ Stars* are being refunded to your Telegram balance.\n\n"
                    f"_(Telegram Stars refunds appear within a few minutes)_"
                ),
                parse_mode="Markdown",
            )
            # Notify loser
            await _bot_app.bot.send_message(
                chat_id=loser_id,
                text=(
                    f"😔 *Challenge resolved*\n\n"
                    f"The {winning_side} side won — better luck next time!\n"
                    f"Your opponent took the pot of *{resolved.pot_stars} ⭐*.\n\n"
                    f"Ready for a rematch? /start"
                ),
                parse_mode="Markdown",
            )
    except Exception as exc:
        logger.error("Failed to resolve/notify challenge %s: %s", challenge.uuid, exc)


async def run_expiry_job() -> None:
    """Every hour: expire old unaccepted challenges."""
    logger.info("Scheduler: running challenge expiry job…")
    async with async_session_factory() as session:
        count = await expire_old_challenges(session)
        if count:
            logger.info("Expired %d stale challenges.", count)


def build_scheduler() -> AsyncIOScheduler:
    """Build and return a configured AsyncIOScheduler (not yet started)."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_match_results, "interval", minutes=5, id="poll_results")
    scheduler.add_job(run_expiry_job, "interval", hours=1, id="expiry")
    return scheduler
