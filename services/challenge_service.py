"""
services/challenge_service.py — Core business logic for P2P challenges.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import CHALLENGE_EXPIRY_HOURS, PHASE_1_FREE, PLATFORM_FEE_PERCENT
from models import Challenge, Match, Transaction, User

logger = logging.getLogger(__name__)


# ── User helpers ──────────────────────────────────────────────────────────────

async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    first_name: str,
    username: str | None = None,
) -> User:
    """Fetch existing user or create a new one."""
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(telegram_id=telegram_id, first_name=first_name, username=username)
        session.add(user)
        await session.flush()
        logger.info("Created new user: %s (%s)", first_name, telegram_id)
    else:
        # Update username in case it changed
        if username:
            user.username = username
    return user


# ── Challenge creation ────────────────────────────────────────────────────────

async def create_challenge(
    session: AsyncSession,
    telegram_id: int,
    first_name: str,
    username: str | None,
    match_id: int,
    creator_side: str,
    amount_stars: int,
    is_public: bool,
) -> Challenge:
    """Create a new open challenge."""
    user = await get_or_create_user(session, telegram_id, first_name, username)
    challenge = Challenge(
        creator_id=user.id,
        match_id=match_id,
        creator_side=creator_side,
        amount_stars=amount_stars,
        is_public=is_public,
        status="open",
    )
    session.add(challenge)
    await session.flush()
    logger.info("Challenge created: %s by user %s", challenge.uuid, telegram_id)
    return challenge


# ── Fetching ──────────────────────────────────────────────────────────────────

async def get_challenge_by_uuid(session: AsyncSession, uuid: str) -> Challenge | None:
    """Load a challenge with its match and creator."""
    result = await session.execute(
        select(Challenge)
        .where(Challenge.uuid == uuid)
        .options(
            selectinload(Challenge.creator),
            selectinload(Challenge.acceptor),
            selectinload(Challenge.match),
        )
    )
    return result.scalar_one_or_none()


async def get_open_market_challenges(
    session: AsyncSession, page: int = 0, per_page: int = 5
) -> list[Challenge]:
    """Return paginated public open challenges."""
    result = await session.execute(
        select(Challenge)
        .where(Challenge.status == "open", Challenge.is_public == True)
        .options(selectinload(Challenge.creator), selectinload(Challenge.match))
        .order_by(Challenge.created_at.desc())
        .offset(page * per_page)
        .limit(per_page)
    )
    return list(result.scalars().all())


async def count_market_challenges(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).where(Challenge.status == "open", Challenge.is_public == True)
    )
    return result.scalar_one()


# ── Payment / escrow ──────────────────────────────────────────────────────────

async def lock_stars_creator(
    session: AsyncSession,
    challenge_uuid: str,
    telegram_charge_id: str,
) -> Challenge:
    """Record that the creator has locked their Stars."""
    challenge = await get_challenge_by_uuid(session, challenge_uuid)
    if not challenge:
        raise ValueError(f"Challenge {challenge_uuid} not found")
    challenge.creator_paid = True
    session.add(Transaction(
        challenge_id=challenge.id,
        user_id=challenge.creator_id,
        type="deposit",
        amount_stars=challenge.amount_stars,
        telegram_payment_charge_id=telegram_charge_id,
    ))
    await _try_lock_challenge(session, challenge)
    return challenge


async def lock_stars_acceptor(
    session: AsyncSession,
    challenge_uuid: str,
    acceptor_telegram_id: int,
    acceptor_first_name: str,
    acceptor_username: str | None,
    telegram_charge_id: str,
) -> Challenge:
    """Record that the acceptor has locked their Stars."""
    challenge = await get_challenge_by_uuid(session, challenge_uuid)
    if not challenge:
        raise ValueError(f"Challenge {challenge_uuid} not found")
    if challenge.status != "open":
        raise ValueError(f"Challenge {challenge_uuid} is no longer open (status={challenge.status})")

    acceptor = await get_or_create_user(
        session, acceptor_telegram_id, acceptor_first_name, acceptor_username
    )
    challenge.acceptor_id = acceptor.id
    challenge.acceptor_paid = True
    challenge.accepted_at = datetime.now(tz=timezone.utc)

    session.add(Transaction(
        challenge_id=challenge.id,
        user_id=acceptor.id,
        type="deposit",
        amount_stars=challenge.amount_stars,
        telegram_payment_charge_id=telegram_charge_id,
    ))
    await _try_lock_challenge(session, challenge)
    return challenge


async def _try_lock_challenge(session: AsyncSession, challenge: Challenge) -> None:
    """Lock the challenge if both sides have paid."""
    if challenge.creator_paid and challenge.acceptor_paid:
        challenge.status = "locked"
        logger.info("Challenge %s is now LOCKED — both sides paid.", challenge.uuid)


# ── Resolution ────────────────────────────────────────────────────────────────

async def resolve_challenge(
    session: AsyncSession,
    challenge_uuid: str,
    winning_side: str,
) -> tuple[Challenge, User, int]:
    """
    Resolve a challenge. Returns (challenge, winner_user, payout_stars).
    winning_side: 'home' | 'draw' | 'away'
    """
    challenge = await get_challenge_by_uuid(session, challenge_uuid)
    if not challenge:
        raise ValueError(f"Challenge {challenge_uuid} not found")
    if challenge.status != "locked":
        raise ValueError(f"Cannot resolve challenge with status={challenge.status}")

    pot = challenge.pot_stars
    fee_stars = 0 if PHASE_1_FREE else int(pot * PLATFORM_FEE_PERCENT)
    payout = pot - fee_stars

    # Determine winner
    if winning_side == challenge.creator_side:
        winner = challenge.creator
        winner_id = challenge.creator_id
    else:
        winner = challenge.acceptor
        winner_id = challenge.acceptor_id

    challenge.status = "resolved"
    challenge.resolved_at = datetime.now(tz=timezone.utc)
    challenge.winner_id = winner_id
    challenge.platform_fee_stars = fee_stars

    # Record payout transaction
    session.add(Transaction(
        challenge_id=challenge.id,
        user_id=winner_id,
        type="withdrawal",
        amount_stars=payout,
        notes=f"Won challenge — {winning_side} side won",
    ))
    if fee_stars:
        session.add(Transaction(
            challenge_id=challenge.id,
            user_id=winner_id,
            type="fee",
            amount_stars=fee_stars,
            notes="2% platform fee",
        ))

    # Update user stats
    winner.total_wins = (winner.total_wins or 0) + 1
    await session.flush()

    logger.info("Challenge %s resolved — winner: %s, payout: %d⭐", challenge.uuid, winner_id, payout)
    return challenge, winner, payout


# ── Expiry ────────────────────────────────────────────────────────────────────

async def expire_old_challenges(session: AsyncSession) -> int:
    """Mark unaccepted open challenges older than CHALLENGE_EXPIRY_HOURS as expired."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=CHALLENGE_EXPIRY_HOURS)
    result = await session.execute(
        select(Challenge).where(
            Challenge.status == "open",
            Challenge.created_at < cutoff,
        )
    )
    challenges = list(result.scalars().all())
    for c in challenges:
        c.status = "expired"
        logger.info("Challenge %s expired.", c.uuid)
    await session.flush()
    return len(challenges)


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_user_stats(session: AsyncSession, telegram_id: int) -> dict:
    """Return stats dict for a user."""
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        return {"found": False}

    created_count = await session.execute(
        select(func.count()).where(Challenge.creator_id == user.id)
    )
    accepted_count = await session.execute(
        select(func.count()).where(Challenge.acceptor_id == user.id)
    )
    resolved_count = await session.execute(
        select(func.count()).where(
            Challenge.winner_id == user.id,
            Challenge.status == "resolved",
        )
    )
    return {
        "found": True,
        "first_name": user.first_name,
        "username": user.username,
        "created": created_count.scalar_one(),
        "accepted": accepted_count.scalar_one(),
        "wins": resolved_count.scalar_one(),
        "total_challenges": user.total_challenges or 0,
    }


async def get_admin_stats(session: AsyncSession) -> dict:
    """Platform-wide stats for /admin command."""
    total = (await session.execute(select(func.count()).select_from(Challenge))).scalar_one()
    active = (await session.execute(
        select(func.count()).where(Challenge.status.in_(["open", "locked"]))
    )).scalar_one()
    resolved = (await session.execute(
        select(func.count()).where(Challenge.status == "resolved")
    )).scalar_one()
    escrow_stars = (await session.execute(
        select(func.sum(Challenge.amount_stars * 2)).where(Challenge.status == "locked")
    )).scalar_one() or 0

    return {
        "total": total,
        "active": active,
        "resolved": resolved,
        "escrow_stars": escrow_stars,
    }
