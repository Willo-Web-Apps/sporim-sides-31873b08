"""
services/challenge_service.py — SideUp Bot Challenge Business Logic
====================================================================
Core business logic for the complete challenge lifecycle:

    create  → open → accept → locked → resolve → done
                                      → cancelled / expired

All database interactions go through the session passed as an argument.
No Telegram dependencies in this module — keep it independently testable.

Public API (used by handlers):
    get_or_create_user()
    get_or_create_match()
    create_challenge()
    get_challenge_by_uuid()
    accept_challenge()
    resolve_challenge()
    get_open_market_challenges()
    get_locked_challenges_for_resolution()
    expire_old_challenges()
    get_platform_stats()
    get_user_stats()
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import config
from models import Challenge, Match, Transaction, User
from services.escrow_service import get_escrow_stats, refund_funds

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Side logic helpers
# ---------------------------------------------------------------------------


def opposite_side(side: str) -> str:
    """
    Compute the acceptor's side given the creator's chosen side.

    Rules:
        - "home"  → acceptor backs "away"
        - "away"  → acceptor backs "home"
        - "draw"  → acceptor backs "against_draw" (wins if result ≠ draw)

    Args:
        side: The creator's chosen side ("home", "away", or "draw").

    Returns:
        The acceptor's logical side as a string.
    """
    mapping = {
        "home": "away",
        "away": "home",
        "draw": "against_draw",
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
    Fetch an existing User by telegram_id or create one if not found.

    Mutates username and first_name on every call to keep them in sync
    with Telegram's current values (users can change their names).

    Args:
        session:     Active AsyncSession.
        telegram_id: Telegram's unique user ID.
        username:    @username string (may be None).
        first_name:  User's first name from the Telegram user object.

    Returns:
        User ORM object (existing or newly created, already flushed).
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
        logger.info(
            "New user created: telegram_id=%d name=%s", telegram_id, first_name
        )
    else:
        # Keep profile data fresh
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
    Fetch an existing Match by external_id or create it from API-supplied data.

    If the match already exists, refresh mutable fields (status, fetched_at)
    in case they have changed since the last sync.

    Args:
        session:    Active AsyncSession.
        match_data: Dict with keys:
                      external_id (str)
                      home_team   (str)
                      away_team   (str)
                      kickoff_time (datetime, UTC-aware)
                      league      (str)  e.g. "PL", "NBA"
                      status      (str, optional)  default "scheduled"

    Returns:
        Match ORM object (existing or newly created, already flushed).
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
        logger.debug(
            "Match created: %s vs %s (%s) external_id=%s",
            match.home_team,
            match.away_team,
            match.league,
            match.external_id,
        )
    else:
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

    Validates all business rules before persisting:
        - creator_side must be "home", "draw", or "away"
        - amount_stars must be within configured bounds
        - match must not have already kicked off

    Args:
        session:       Active AsyncSession.
        creator:       User creating the challenge.
        match:         Match the challenge is placed on.
        creator_side:  "home" | "draw" | "away"
        amount_stars:  Stars each side must deposit.
        is_public:     If True, visible in The Market for anyone to accept.

    Returns:
        Newly created Challenge with status="open".

    Raises:
        ValueError: On any validation failure.
    """
    if creator_side not in ("home", "draw", "away"):
        raise ValueError(
            f"Invalid side '{creator_side}'. Must be 'home', 'draw', or 'away'."
        )

    if not (config.MIN_CHALLENGE_AMOUNT <= amount_stars <= config.MAX_CHALLENGE_AMOUNT):
        raise ValueError(
            f"Amount {amount_stars} Stars is out of range "
            f"({config.MIN_CHALLENGE_AMOUNT}–{config.MAX_CHALLENGE_AMOUNT:,})."
        )

    now = datetime.now(timezone.utc)
    kickoff = match.kickoff_time
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)

    if kickoff <= now:
        raise ValueError(
            "Cannot create a challenge for a match that has already started or finished."
        )

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
        "Challenge created: uuid=%s creator=%d side=%s match=%s %d⭐ public=%s",
        challenge.uuid,
        creator.telegram_id,
        creator_side,
        f"{match.home_team} vs {match.away_team}",
        amount_stars,
        is_public,
    )
    return challenge


# ---------------------------------------------------------------------------
# Challenge retrieval
# ---------------------------------------------------------------------------


async def get_challenge_by_uuid(
    session: AsyncSession,
    uuid: str,
) -> Optional[Challenge]:
    """
    Fetch a single challenge by UUID with all relationships eagerly loaded.

    Args:
        session: Active AsyncSession.
        uuid:    Challenge UUID (32-char hex, from invite link).

    Returns:
        Challenge with creator, acceptor, match, winner loaded — or None.
    """
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


# ---------------------------------------------------------------------------
# Stars locking (post-payment)
# ---------------------------------------------------------------------------


async def lock_stars_creator(
    session: AsyncSession,
    challenge_uuid: str,
    telegram_charge_id: str,
) -> Challenge:
    """
    Record the creator's Stars deposit after Telegram confirms payment.

    If the acceptor has already paid, moves the challenge to 'locked'.

    Args:
        session:             Active AsyncSession.
        challenge_uuid:      UUID of the challenge.
        telegram_charge_id:  Telegram's charge ID for the payment.

    Returns:
        Updated Challenge object.

    Raises:
        ValueError: If challenge not found or not in open status.
    """
    challenge = await get_challenge_by_uuid(session, challenge_uuid)
    if challenge is None:
        raise ValueError(f"Challenge {challenge_uuid} not found.")
    if challenge.status != "open":
        raise ValueError(f"Challenge is {challenge.status}, cannot lock creator.")
    if challenge.creator is None:
        raise ValueError("Challenge has no creator record.")

    from services.escrow_service import check_both_sides_paid, lock_funds

    await lock_funds(
        session=session,
        challenge_id=challenge.id,
        user_id=challenge.creator.id,
        payment_charge_id=telegram_charge_id,
        amount_stars=challenge.amount_stars,
    )

    both_paid = await check_both_sides_paid(session, challenge)
    if both_paid:
        challenge.status = "locked"
        logger.info("Challenge %s locked (creator paid, acceptor already paid).", challenge_uuid)

    return challenge


async def lock_stars_acceptor(
    session: AsyncSession,
    challenge_uuid: str,
    acceptor_user: User,
    telegram_charge_id: str,
) -> Challenge:
    """
    Record the acceptor's Stars deposit after Telegram confirms payment.

    If the creator has already paid, moves the challenge to 'locked'.

    Args:
        session:             Active AsyncSession.
        challenge_uuid:      UUID of the challenge.
        acceptor_user:       User ORM object for the acceptor.
        telegram_charge_id:  Telegram's charge ID for the payment.

    Returns:
        Updated Challenge object.

    Raises:
        ValueError: If challenge not found, not open, or user is creator.
    """
    challenge = await get_challenge_by_uuid(session, challenge_uuid)
    if challenge is None:
        raise ValueError(f"Challenge {challenge_uuid} not found.")
    if challenge.status != "open":
        raise ValueError(f"Challenge is {challenge.status}, cannot lock acceptor.")
    if challenge.creator and challenge.creator.id == acceptor_user.id:
        raise ValueError("You cannot accept your own challenge.")

    from services.escrow_service import check_both_sides_paid, lock_funds

    await lock_funds(
        session=session,
        challenge_id=challenge.id,
        user_id=acceptor_user.id,
        payment_charge_id=telegram_charge_id,
        amount_stars=challenge.amount_stars,
    )

    both_paid = await check_both_sides_paid(session, challenge)
    if both_paid:
        challenge.status = "locked"
        logger.info("Challenge %s locked (acceptor paid, creator already paid).", challenge_uuid)

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
    Register a user as the acceptor of an open challenge.

    The challenge status remains 'open' until payment is confirmed.
    This records intent to accept — actual lock happens in payment handler.

    Args:
        session:        Active AsyncSession.
        challenge_uuid: UUID of the challenge to accept.
        acceptor:       User who wants to take the opposite side.

    Returns:
        Updated Challenge with acceptor set (status still 'open').

    Raises:
        ValueError: Challenge not found, not open, user is creator,
                    or match already started.
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
            f"Challenge is {challenge.status}. "
            "It may already be accepted, locked, or expired."
        )

    if challenge.creator_id == acceptor.id:
        raise ValueError("You cannot accept your own challenge!")

    now = datetime.now(timezone.utc)
    kickoff = challenge.match.kickoff_time
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)

    if kickoff <= now:
        challenge.status = "expired"
        raise ValueError("The match has already started. This challenge has expired.")

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
    Resolve a locked challenge — determine the winner and calculate payout.

    Args:
        session:     Active AsyncSession.
        challenge:   The Challenge to resolve (must have status='locked').
        winner_side: The winning outcome: "home", "draw", or "away".

    Returns:
        Tuple of (winner_user, payout_stars, fee_stars).

    Raises:
        ValueError: Challenge not locked, or winner cannot be determined.
    """
    if challenge.status != "locked":
        raise ValueError(
            f"Challenge must be 'locked' to resolve. Current: {challenge.status}"
        )

    # Ensure relationships are loaded
    if not challenge.creator:
        await session.refresh(challenge, attribute_names=["creator", "acceptor", "match"])

    winner: Optional[User] = None

    creator_side = challenge.creator_side
    acceptor_side = challenge.acceptor_side

    # Direct match
    if creator_side == winner_side:
        winner = challenge.creator
    elif acceptor_side == winner_side:
        winner = challenge.acceptor
    # against_draw: acceptor wins if result is not a draw
    elif acceptor_side == "against_draw" and winner_side != "draw":
        winner = challenge.acceptor
    # If creator picked draw and it was a draw
    elif creator_side == "draw" and winner_side == "draw":
        winner = challenge.creator

    if winner is None:
        raise ValueError(
            f"Cannot determine winner. "
            f"creator_side={creator_side}, acceptor_side={acceptor_side}, "
            f"match_result={winner_side}"
        )

    total_pot = challenge.total_pot
    fee_stars = int(total_pot * config.PLATFORM_FEE_PERCENT)
    payout_stars = total_pot - fee_stars

    challenge.status = "resolved"
    challenge.winner_id = winner.id
    challenge.platform_fee_stars = fee_stars
    challenge.resolved_at = datetime.now(timezone.utc)
    winner.total_wins += 1

    await session.flush()

    logger.info(
        "Challenge %s resolved: winner=%d result=%s payout=%d⭐ fee=%d⭐",
        challenge.uuid,
        winner.id,
        winner_side,
        payout_stars,
        fee_stars,
    )
    return winner, payout_stars, fee_stars


# ---------------------------------------------------------------------------
# Market queries
# ---------------------------------------------------------------------------


async def get_open_market_challenges(
    session: AsyncSession,
    page: int = 0,
    per_page: int = config.MARKET_PAGE_SIZE,
) -> tuple[list[Challenge], int]:
    """
    Fetch a paginated list of open public challenges for The Market.

    Only shows challenges where:
        - is_public = True
        - status = 'open'
        - match kickoff is in the future

    Args:
        session:  Active AsyncSession.
        page:     Zero-based page index.
        per_page: Items per page (default from config.MARKET_PAGE_SIZE).

    Returns:
        Tuple of (challenges, total_count).
    """
    now = datetime.now(timezone.utc)
    base_q = (
        select(Challenge)
        .where(
            and_(
                Challenge.is_public == True,
                Challenge.status == "open",
                Challenge.match.has(Match.kickoff_time > now),
            )
        )
        .options(
            selectinload(Challenge.creator),
            selectinload(Challenge.match),
        )
        .order_by(Challenge.created_at.desc())
    )

    count_result = await session.execute(
        select(func.count()).select_from(base_q.subquery())
    )
    total = count_result.scalar_one()

    result = await session.execute(
        base_q.offset(page * per_page).limit(per_page)
    )
    challenges = list(result.scalars().all())

    return challenges, total


# ---------------------------------------------------------------------------
# Scheduler support queries
# ---------------------------------------------------------------------------


async def get_locked_challenges_for_resolution(
    session: AsyncSession,
) -> list[Challenge]:
    """
    Fetch all locked challenges where the match kickoff time has passed.

    Called by the APScheduler result-polling job every 5 minutes.

    Args:
        session: Active AsyncSession.

    Returns:
        List of locked Challenge objects with creator, acceptor, match loaded.
    """
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(Challenge)
        .where(
            and_(
                Challenge.status == "locked",
                Challenge.match.has(Match.kickoff_time < now),
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
    Mark open challenges that are older than CHALLENGE_EXPIRY_HOURS as expired.

    Does NOT issue refunds (no Stars have been locked for purely 'open'
    challenges — payment only happens after acceptance). Simply sets status.

    Args:
        session: Active AsyncSession.

    Returns:
        Number of challenges expired in this call.
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
        logger.info("Expired %d stale open challenges (older than %dh).", count, config.CHALLENGE_EXPIRY_HOURS)

    return count


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


async def get_platform_stats(session: AsyncSession) -> dict:
    """
    Aggregate platform-wide statistics for the admin dashboard.

    Returns:
        Dict with keys:
            total_users, total_challenges,
            open_challenges, locked_challenges,
            resolved_challenges, cancelled_challenges,
            total_volume_stars, total_fees_stars
    """
    # Challenge counts by status
    counts_result = await session.execute(
        select(Challenge.status, func.count(Challenge.id)).group_by(Challenge.status)
    )
    status_counts: dict[str, int] = dict(counts_result.all())

    # User count
    user_count_result = await session.execute(select(func.count(User.id)))
    user_count = user_count_result.scalar_one()

    # Escrow stats
    escrow = await get_escrow_stats(session)

    return {
        "total_users": user_count,
        "total_challenges": sum(status_counts.values()),
        "open_challenges": status_counts.get("open", 0),
        "locked_challenges": status_counts.get("locked", 0),
        "resolved_challenges": status_counts.get("resolved", 0),
        "cancelled_challenges": (
            status_counts.get("cancelled", 0) + status_counts.get("expired", 0)
        ),
        **escrow,
    }


async def get_user_stats(session: AsyncSession, telegram_id: int) -> dict:
    """
    Fetch per-user statistics for the /stats command.

    Args:
        session:     Active AsyncSession.
        telegram_id: The user's Telegram ID.

    Returns:
        Dict with keys:
            total_challenges, total_wins, win_rate,
            open_challenges, locked_challenges, total_stars_wagered
        Returns empty/zero dict if user not found.
    """
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        return {
            "total_challenges": 0,
            "total_wins": 0,
            "win_rate": 0.0,
            "open_challenges": 0,
            "locked_challenges": 0,
            "total_stars_wagered": 0,
        }

    # Count challenges by status for this user
    created_q = select(Challenge.status, func.count(Challenge.id)).where(
        Challenge.creator_id == user.id
    ).group_by(Challenge.status)

    accepted_q = select(Challenge.status, func.count(Challenge.id)).where(
        Challenge.acceptor_id == user.id
    ).group_by(Challenge.status)

    created_result = await session.execute(created_q)
    accepted_result = await session.execute(accepted_q)

    created_counts: dict[str, int] = dict(created_result.all())
    accepted_counts: dict[str, int] = dict(accepted_result.all())

    # Merge counts
    all_statuses = set(created_counts) | set(accepted_counts)
    status_totals = {
        s: created_counts.get(s, 0) + accepted_counts.get(s, 0)
        for s in all_statuses
    }

    total = sum(status_totals.values())
    wins = user.total_wins
    win_rate = round(wins / total * 100, 1) if total > 0 else 0.0

    # Total Stars wagered (sum of deposits)
    stars_result = await session.execute(
        select(func.sum(Transaction.amount_stars)).where(
            Transaction.user_id == user.id,
            Transaction.type == "deposit",
        )
    )
    total_wagered = stars_result.scalar_one() or 0

    return {
        "total_challenges": total,
        "total_wins": wins,
        "win_rate": win_rate,
        "open_challenges": status_totals.get("open", 0),
        "locked_challenges": status_totals.get("locked", 0),
        "total_stars_wagered": total_wagered,
    }
