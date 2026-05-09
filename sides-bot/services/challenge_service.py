"""
services/challenge_service.py — SIDES Bot Challenge Business Logic
===================================================================
Core business logic for the challenge lifecycle:
    create  → open → accept → locked → resolve → done
                                                → cancelled / expired

All database interactions go through the session passed as an argument.
No Telegram dependencies in this module — keep it testable.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import config
from models import Challenge, Match, Transaction, User
from services.escrow_service import refund_funds

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Side logic helpers
# ---------------------------------------------------------------------------

def opposite_side(side: str) -> str:
    """
    Compute the acceptor's side given the creator's side.

    Rules:
        - If creator picks 'home', acceptor picks 'away'
        - If creator picks 'away', acceptor picks 'home'
        - If creator picks 'draw', acceptor picks either team
          (stored as 'draw_opponent' — acceptor must choose at accept time)

    For V1 simplicity: if creator picks draw, acceptor backs
    the field (i.e., either home or away wins). Stored as 'against_draw'.
    """
    mapping = {
        "home": "away",
        "away": "home",
        "draw": "against_draw",  # Acceptor wins if it's NOT a draw
    }
    return mapping.get(side, "away")


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: Optional[str],
    first_name: str,
) -> User:
    """
    Get existing user or create a new one. Upsert pattern.

    Args:
        session:     Async DB session
        telegram_id: Telegram user ID
        username:    @username (may be None)
        first_name:  User's first name

    Returns:
        User ORM object (existing or newly created)
    """
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
        session.add(user)
        await session.flush()
        logger.info("Created new user: telegram_id=%d name=%s", telegram_id, first_name)
    else:
        # Update mutable fields in case they changed
        user.username = username
        user.first_name = first_name

    return user


# ---------------------------------------------------------------------------
# Match management
# ---------------------------------------------------------------------------

async def get_or_create_match(
    session: AsyncSession,
    match_data: dict,
) -> Match:
    """
    Get existing match by external_id or create it from API data.

    Args:
        session:    Async DB session
        match_data: Dict from sports_api with keys:
                    external_id, home_team, away_team,
                    kickoff_time, league, status

    Returns:
        Match ORM object
    """
    result = await session.execute(
        select(Match).where(Match.external_id == match_data["external_id"])
    )
    match = result.scalar_one_or_none()

    if match is None:
        match = Match(
            external_id=match_data["external_id"],
            league=match_data["league"],
            home_team=match_data["home_team"],
            away_team=match_data["away_team"],
            kickoff_time=match_data["kickoff_time"],
            status=match_data.get("status", "scheduled"),
        )
        session.add(match)
        await session.flush()
    else:
        # Refresh data in case status changed
        match.status = match_data.get("status", match.status)
        match.fetched_at = datetime.now(timezone.utc)

    return match


# ---------------------------------------------------------------------------
# Challenge creation
# ---------------------------------------------------------------------------

async def create_challenge(
    session: AsyncSession,
    creator: User,
    match: Match,
    creator_side: str,
    amount_stars: int,
    is_public: bool = False,
) -> Challenge:
    """
    Create a new open challenge.

    Args:
        session:       Async DB session
        creator:       User creating the challenge
        match:         Match the challenge is on
        creator_side:  "home" | "draw" | "away"
        amount_stars:  Stars each side must deposit
        is_public:     Whether to show in The Market

    Returns:
        Newly created Challenge (status=open)

    Raises:
        ValueError: If amount is outside allowed range or side is invalid
    """
    if creator_side not in ("home", "draw", "away"):
        raise ValueError(f"Invalid side: {creator_side}. Must be home, draw, or away.")

    if not (config.MIN_CHALLENGE_AMOUNT <= amount_stars <= config.MAX_CHALLENGE_AMOUNT):
        raise ValueError(
            f"Amount {amount_stars} is outside allowed range "
            f"({config.MIN_CHALLENGE_AMOUNT}–{config.MAX_CHALLENGE_AMOUNT} Stars)"
        )

    if match.kickoff_time <= datetime.now(timezone.utc):
        raise ValueError("Cannot create a challenge for a match that has already started.")

    challenge = Challenge(
        creator_id=creator.id,
        match_id=match.id,
        creator_side=creator_side,
        amount_stars=amount_stars,
        is_public=is_public,
        status="open",
    )
    session.add(challenge)
    creator.total_challenges += 1
    await session.flush()

    logger.info(
        "Created challenge %s: user %d backs %s in %s vs %s (%d ⭐, public=%s)",
        challenge.uuid,
        creator.id,
        creator_side,
        match.home_team,
        match.away_team,
        amount_stars,
        is_public,
    )
    return challenge


# ---------------------------------------------------------------------------
# Challenge acceptance
# ---------------------------------------------------------------------------

async def accept_challenge(
    session: AsyncSession,
    challenge_uuid: str,
    acceptor: User,
) -> Challenge:
    """
    Accept an open challenge.

    Args:
        session:        Async DB session
        challenge_uuid: Challenge UUID from the invite link
        acceptor:       User accepting the challenge

    Returns:
        Updated Challenge (status remains 'open' until payment confirmed)

    Raises:
        ValueError: If challenge not found, already accepted, or user is creator
    """
    result = await session.execute(
        select(Challenge)
        .where(Challenge.uuid == challenge_uuid)
        .options(
            selectinload(Challenge.creator),
            selectinload(Challenge.match),
        )
    )
    challenge = result.scalar_one_or_none()

    if challenge is None:
        raise ValueError(f"Challenge {challenge_uuid} not found.")

    if challenge.status != "open":
        raise ValueError(
            f"Challenge is {challenge.status}, not open. "
            "It may have already been accepted, cancelled, or expired."
        )

    if challenge.creator_id == acceptor.id:
        raise ValueError("You cannot accept your own challenge!")

    if challenge.match.kickoff_time <= datetime.now(timezone.utc):
        challenge.status = "expired"
        raise ValueError("This match has already started. Challenge is now expired.")

    # Set acceptor and compute their side
    challenge.acceptor_id = acceptor.id
    challenge.acceptor_side = opposite_side(challenge.creator_side)
    challenge.accepted_at = datetime.now(timezone.utc)
    acceptor.total_challenges += 1

    await session.flush()

    logger.info(
        "Challenge %s accepted by user %d (side: %s)",
        challenge_uuid,
        acceptor.id,
        challenge.acceptor_side,
    )
    return challenge


# ---------------------------------------------------------------------------
# Challenge resolution
# ---------------------------------------------------------------------------

async def resolve_challenge(
    session: AsyncSession,
    challenge: Challenge,
    winner_side: str,
) -> tuple[User, int, int]:
    """
    Resolve a locked challenge — determine winner and calculate payout.

    Args:
        session:     Async DB session
        challenge:   Locked Challenge to resolve
        winner_side: "home" | "draw" | "away" — the winning outcome

    Returns:
        Tuple of (winner_user, payout_stars, fee_stars)

    Raises:
        ValueError: If challenge is not locked, or sides are invalid
    """
    if challenge.status != "locked":
        raise ValueError(
            f"Challenge must be 'locked' to resolve. Current status: {challenge.status}"
        )

    # Load relationships if not loaded
    if not challenge.creator:
        await session.refresh(challenge, ["creator", "acceptor", "match"])

    # Determine winner
    winner: Optional[User] = None

    if challenge.creator_side == winner_side:
        winner = challenge.creator
    elif challenge.acceptor_side == winner_side:
        winner = challenge.acceptor
    elif challenge.creator_side == "draw" and winner_side == "draw":
        winner = challenge.creator
    elif challenge.acceptor_side == "against_draw" and winner_side != "draw":
        winner = challenge.acceptor
    else:
        # Edge case: if draw and creator picked draw → creator wins
        # If creator picked a team and it drew → acceptor wins (against_draw)
        pass

    if winner is None:
        raise ValueError(
            f"Could not determine winner. Creator side: {challenge.creator_side}, "
            f"Acceptor side: {challenge.acceptor_side}, Match result: {winner_side}"
        )

    # Calculate payout
    total_pot = challenge.total_pot
    fee_stars = int(total_pot * config.PLATFORM_FEE_PERCENT)
    payout_stars = total_pot - fee_stars

    # Update challenge
    challenge.status = "resolved"
    challenge.winner_id = winner.id
    challenge.platform_fee_stars = fee_stars
    challenge.resolved_at = datetime.now(timezone.utc)

    # Update winner stats
    winner.total_wins += 1

    await session.flush()

    logger.info(
        "Resolved challenge %s: winner %d (%s), payout %d ⭐, fee %d ⭐",
        challenge.uuid,
        winner.id,
        winner_side,
        payout_stars,
        fee_stars,
    )
    return winner, payout_stars, fee_stars


# ---------------------------------------------------------------------------
# Challenge cancellation
# ---------------------------------------------------------------------------

async def cancel_challenge(
    session: AsyncSession,
    challenge_uuid: str,
    requesting_user: User,
) -> Challenge:
    """
    Cancel an open challenge. Only the creator can cancel before acceptance.

    Args:
        session:          Async DB session
        challenge_uuid:   Challenge UUID
        requesting_user:  User requesting cancellation

    Returns:
        Updated Challenge (status=cancelled)

    Raises:
        ValueError: If not found, not open, or user is not the creator
    """
    result = await session.execute(
        select(Challenge).where(Challenge.uuid == challenge_uuid)
    )
    challenge = result.scalar_one_or_none()

    if challenge is None:
        raise ValueError(f"Challenge {challenge_uuid} not found.")

    if challenge.creator_id != requesting_user.id:
        raise ValueError("Only the challenge creator can cancel it.")

    if challenge.status not in ("open",):
        raise ValueError(
            f"Challenge cannot be cancelled in status '{challenge.status}'. "
            "Only open challenges can be cancelled."
        )

    challenge.status = "cancelled"
    await session.flush()

    logger.info(
        "Challenge %s cancelled by user %d",
        challenge_uuid,
        requesting_user.id,
    )
    return challenge


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

async def get_challenge_by_uuid(
    session: AsyncSession,
    uuid: str,
) -> Optional[Challenge]:
    """Fetch a challenge with all relationships loaded."""
    result = await session.execute(
        select(Challenge)
        .where(Challenge.uuid == uuid)
        .options(
            selectinload(Challenge.creator),
            selectinload(Challenge.acceptor),
            selectinload(Challenge.match),
            selectinload(Challenge.winner),
        )
    )
    return result.scalar_one_or_none()


async def get_open_market_challenges(
    session: AsyncSession,
    page: int = 0,
    page_size: int = config.MARKET_PAGE_SIZE,
) -> tuple[list[Challenge], int]:
    """
    Fetch paginated open public challenges for The Market.

    Returns:
        Tuple of (challenges_on_page, total_count)
    """
    base_query = (
        select(Challenge)
        .where(
            and_(
                Challenge.is_public == True,
                Challenge.status == "open",
                Challenge.match.has(
                    Match.kickoff_time > datetime.now(timezone.utc)
                ),
            )
        )
        .options(
            selectinload(Challenge.creator),
            selectinload(Challenge.match),
        )
        .order_by(Challenge.created_at.desc())
    )

    # Total count
    count_result = await session.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar_one()

    # Paginated results
    result = await session.execute(
        base_query.offset(page * page_size).limit(page_size)
    )
    challenges = list(result.scalars().all())

    return challenges, total


async def get_locked_challenges_for_resolution(
    session: AsyncSession,
) -> list[Challenge]:
    """
    Fetch all locked challenges where the match has already kicked off.
    Called by the APScheduler job every 15 minutes.
    """
    result = await session.execute(
        select(Challenge)
        .where(
            and_(
                Challenge.status == "locked",
                Challenge.match.has(
                    Match.kickoff_time < datetime.now(timezone.utc)
                ),
            )
        )
        .options(
            selectinload(Challenge.creator),
            selectinload(Challenge.acceptor),
            selectinload(Challenge.match),
        )
    )
    return list(result.scalars().all())


async def expire_old_challenges(session: AsyncSession) -> int:
    """
    Mark open challenges older than CHALLENGE_EXPIRY_HOURS as expired.
    Returns number of challenges expired.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.CHALLENGE_EXPIRY_HOURS)

    result = await session.execute(
        select(Challenge).where(
            and_(
                Challenge.status == "open",
                Challenge.created_at < cutoff,
            )
        )
    )
    challenges = result.scalars().all()

    count = 0
    for challenge in challenges:
        challenge.status = "expired"
        count += 1

    if count:
        await session.flush()
        logger.info("Expired %d stale challenges", count)

    return count


async def get_platform_stats(session: AsyncSession) -> dict:
    """Aggregate platform statistics for admin dashboard."""
    from services.escrow_service import get_escrow_stats

    # Challenge counts by status
    counts_result = await session.execute(
        select(Challenge.status, func.count(Challenge.id))
        .group_by(Challenge.status)
    )
    status_counts = dict(counts_result.all())

    # User count
    user_count_result = await session.execute(select(func.count(User.id)))
    user_count = user_count_result.scalar_one()

    # Escrow stats
    escrow_stats = await get_escrow_stats(session)

    return {
        "total_users": user_count,
        "total_challenges": sum(status_counts.values()),
        "open_challenges": status_counts.get("open", 0),
        "locked_challenges": status_counts.get("locked", 0),
        "resolved_challenges": status_counts.get("resolved", 0),
        "cancelled_challenges": (
            status_counts.get("cancelled", 0) + status_counts.get("expired", 0)
        ),
        **escrow_stats,
    }
