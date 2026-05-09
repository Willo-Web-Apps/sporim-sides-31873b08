"""
services/escrow_service.py — SIDES Bot Escrow Service
=======================================================
Tracks the movement of Telegram Stars between users and the platform.

IMPORTANT — Telegram Stars V1 Limitation:
    As of 2025, Telegram supports:
        ✅ Bot receiving Stars from users (via invoice/payment)
        ❌ Bot sending Stars to users (NOT supported by Telegram API yet)

    V1 WORKAROUND:
        - Deposits: Users pay via Telegram Stars invoice (automatic)
        - Payouts: Admin is notified and must manually refund via
                   Telegram's refund mechanism or direct transfer
        - All fund movements are recorded in the transactions table
          as the authoritative source of truth

    When Telegram enables bot-initiated Star transfers, this service
    will be updated to send Stars programmatically.

    Relevant Telegram docs:
        https://core.telegram.org/bots/api#refundstarpayment
        (Refund is available — but is per payment, not arbitrary transfer)
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models import Challenge, Transaction, User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fund Locking (Deposit)
# ---------------------------------------------------------------------------

async def lock_funds(
    session: AsyncSession,
    challenge_id: int,
    user_id: int,
    payment_charge_id: str,
    amount_stars: int,
) -> Transaction:
    """
    Record a user's deposit into a challenge escrow.

    Called after Telegram confirms a successful Stars payment.
    The payment_charge_id is stored for potential Telegram refunds.

    Args:
        session:             Async DB session
        challenge_id:        Challenge.id (not UUID)
        user_id:             User.id (internal PK)
        payment_charge_id:   Telegram's telegram_payment_charge_id
        amount_stars:        Number of Stars deposited

    Returns:
        The created Transaction record
    """
    tx = Transaction(
        challenge_id=challenge_id,
        user_id=user_id,
        type="deposit",
        amount_stars=amount_stars,
        telegram_payment_charge_id=payment_charge_id,
    )
    session.add(tx)
    await session.flush()

    logger.info(
        "Locked %d ⭐ for user %d in challenge %d (charge: %s)",
        amount_stars,
        user_id,
        challenge_id,
        payment_charge_id,
    )
    return tx


async def check_both_sides_paid(
    session: AsyncSession,
    challenge: Challenge,
) -> bool:
    """
    Check whether both the creator and acceptor have deposited Stars.

    Returns True if the challenge should be moved to 'locked' status.
    """
    result = await session.execute(
        select(func.count(Transaction.id)).where(
            Transaction.challenge_id == challenge.id,
            Transaction.type == "deposit",
        )
    )
    deposit_count = result.scalar_one()
    # Need exactly 2 deposits (one from each side)
    return deposit_count >= 2


# ---------------------------------------------------------------------------
# Fund Release (Payout)
# ---------------------------------------------------------------------------

async def release_funds(
    session: AsyncSession,
    challenge: Challenge,
    winner: User,
    payout_amount: int,
    fee_amount: int,
) -> None:
    """
    Record the payout to the winner and platform fee.

    ⚠️  V1 LIMITATION: This records INTENT to pay. Actual Stars transfer
        must be done manually by admin via Telegram refund API or future
        bot-to-user transfer API.

    When Telegram enables bot-initiated transfers, replace the TODO below
    with an actual API call to send Stars to winner.telegram_id.

    Args:
        session:        Async DB session
        challenge:      Resolved Challenge object
        winner:         User who won
        payout_amount:  Stars to pay winner (pot minus fee)
        fee_amount:     Platform fee Stars
    """
    # Record the payout transaction
    payout_tx = Transaction(
        challenge_id=challenge.id,
        user_id=winner.id,
        type="withdrawal",
        amount_stars=payout_amount,
        telegram_payment_charge_id=None,
    )
    session.add(payout_tx)

    # Record the platform fee (if any)
    if fee_amount > 0:
        fee_tx = Transaction(
            challenge_id=challenge.id,
            user_id=winner.id,  # Attributed to winner for accounting
            type="fee",
            amount_stars=fee_amount,
            telegram_payment_charge_id=None,
        )
        session.add(fee_tx)

    await session.flush()

    logger.info(
        "Recorded payout: %d ⭐ to user %d, fee %d ⭐ (challenge %d)",
        payout_amount,
        winner.id,
        fee_amount,
        challenge.id,
    )

    # TODO: When Telegram supports bot → user Star transfers, send here:
    # await context.bot.send_stars(
    #     user_id=winner.telegram_id,
    #     amount=payout_amount,
    #     payload=f"payout_challenge_{challenge.uuid}",
    # )
    #
    # For now, admin must use /resolve and manually transfer Stars.
    # The winner is notified in the challenge resolution handler.


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

async def refund_funds(
    session: AsyncSession,
    challenge: Challenge,
    user: User,
    amount_stars: int,
) -> Transaction:
    """
    Record a refund to a user (cancellation or expiry).

    ⚠️  V1 LIMITATION: Records the refund in the DB. Admin must execute
        the actual Telegram Stars refund via the Bot API:
        https://core.telegram.org/bots/api#refundstarpayment

    The telegram_payment_charge_id from the original deposit transaction
    is needed to issue a Telegram refund. Fetch it before calling this.

    Args:
        session:      Async DB session
        challenge:    Challenge being refunded
        user:         User receiving the refund
        amount_stars: Stars to refund

    Returns:
        The created refund Transaction
    """
    tx = Transaction(
        challenge_id=challenge.id,
        user_id=user.id,
        type="refund",
        amount_stars=amount_stars,
        telegram_payment_charge_id=None,
    )
    session.add(tx)
    await session.flush()

    logger.info(
        "Recorded refund: %d ⭐ to user %d (challenge %d)",
        amount_stars,
        user.id,
        challenge.id,
    )
    return tx


async def get_deposit_charge_id(
    session: AsyncSession,
    challenge_id: int,
    user_id: int,
) -> str | None:
    """
    Retrieve the Telegram payment charge ID for a user's deposit.
    Needed to issue a Telegram Stars refund via refundStarPayment.
    """
    result = await session.execute(
        select(Transaction.telegram_payment_charge_id).where(
            Transaction.challenge_id == challenge_id,
            Transaction.user_id == user_id,
            Transaction.type == "deposit",
        ).limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

async def get_escrow_stats(session: AsyncSession) -> dict:
    """
    Aggregate escrow statistics for the admin /stats command.
    """
    # Total Stars deposited
    deposit_result = await session.execute(
        select(func.sum(Transaction.amount_stars)).where(
            Transaction.type == "deposit"
        )
    )
    total_deposited = deposit_result.scalar_one() or 0

    # Total fees collected
    fee_result = await session.execute(
        select(func.sum(Transaction.amount_stars)).where(
            Transaction.type == "fee"
        )
    )
    total_fees = fee_result.scalar_one() or 0

    return {
        "total_volume_stars": total_deposited,
        "total_fees_stars": total_fees,
    }
