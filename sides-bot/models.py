"""
models.py — SIDES Bot Database Models
=======================================
SQLAlchemy ORM models using the declarative base pattern.
All models are compatible with async SQLAlchemy (aiosqlite driver).

Models:
    User        — Telegram users who interact with the bot
    Match       — Sports matches fetched from external APIs
    Challenge   — A peer-to-peer challenge between two users
    Transaction — Financial record of Stars movements
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    """Generate a URL-safe UUID string for challenge invite links."""
    return uuid.uuid4().hex  # 32-char hex, no hyphens, URL-safe


# ---------------------------------------------------------------------------
# User Model
# ---------------------------------------------------------------------------

class User(Base):
    """
    A Telegram user who has interacted with the bot.

    Created automatically on first /start. Used as FK in challenges
    and transactions to track per-user statistics.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        nullable=False,
        index=True,
        comment="Telegram user ID (from update.effective_user.id)",
    )
    username: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="Telegram @username (may be None if user has no username)",
    )
    first_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
        comment="Telegram first_name from user object",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    total_challenges: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Total challenges created + accepted by this user",
    )
    total_wins: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Total challenges won by this user",
    )

    # Relationships
    created_challenges: Mapped[list["Challenge"]] = relationship(
        "Challenge",
        foreign_keys="Challenge.creator_id",
        back_populates="creator",
        lazy="select",
    )
    accepted_challenges: Mapped[list["Challenge"]] = relationship(
        "Challenge",
        foreign_keys="Challenge.acceptor_id",
        back_populates="acceptor",
        lazy="select",
    )
    won_challenges: Mapped[list["Challenge"]] = relationship(
        "Challenge",
        foreign_keys="Challenge.winner_id",
        back_populates="winner",
        lazy="select",
    )
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction",
        back_populates="user",
        lazy="select",
    )

    def display_name(self) -> str:
        """Human-readable name: @username if available, else first_name."""
        if self.username:
            return f"@{self.username}"
        return self.first_name or f"User#{self.telegram_id}"

    def __repr__(self) -> str:
        return f"<User id={self.id} telegram_id={self.telegram_id} name={self.display_name()}>"


# ---------------------------------------------------------------------------
# Match Model
# ---------------------------------------------------------------------------

class Match(Base):
    """
    A sports match fetched from an external API (football-data.org or Ball Don't Lie).

    Cached locally to avoid repeated API calls and to preserve
    historical match data after results are confirmed.
    """
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        comment="ID from the external API (e.g. football-data.org match ID)",
    )
    league: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="League code: PL, CL, WC, NBA",
    )
    home_team: Mapped[str] = mapped_column(String(128), nullable=False)
    away_team: Mapped[str] = mapped_column(String(128), nullable=False)
    kickoff_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Scheduled kickoff / tip-off time in UTC",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="scheduled",
        nullable=False,
        comment="scheduled | live | finished | postponed | cancelled",
    )
    home_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    away_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    winner: Mapped[str] = mapped_column(
        String(8),
        default="tbd",
        nullable=False,
        comment="home | draw | away | tbd (set after match finishes)",
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
        comment="When this record was last synced from the external API",
    )

    # Relationships
    challenges: Mapped[list["Challenge"]] = relationship(
        "Challenge",
        back_populates="match",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<Match id={self.id} {self.home_team} vs {self.away_team} "
            f"({self.league}) @ {self.kickoff_time.strftime('%Y-%m-%d %H:%M')}>"
        )


# ---------------------------------------------------------------------------
# Challenge Model
# ---------------------------------------------------------------------------

class Challenge(Base):
    """
    A peer-to-peer sports challenge between two users.

    Lifecycle:
        open       → created, waiting for acceptor (funds not yet locked)
        locked     → both sides have deposited Stars (funds held in escrow)
        resolved   → match finished, winner paid out
        cancelled  → creator cancelled before acceptance / both refunded
        expired    → 48h passed with no acceptor, refund issued

    The invite link is: https://t.me/{BOT_USERNAME}?start=ref_{uuid}
    """
    __tablename__ = "challenges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
        default=_new_uuid,
        index=True,
        comment="URL-safe UUID used in deep-link invite URLs",
    )
    creator_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    acceptor_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
        comment="Set when a second user accepts the challenge",
    )
    match_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("matches.id"),
        nullable=False,
    )
    creator_side: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="home | draw | away — the side the creator is backing",
    )
    # acceptor_side is the logical opposite of creator_side.
    # home ↔ away (draw is only accepted as-is when creator picks draw,
    # acceptor must pick one of the two teams).
    # Stored explicitly for query simplicity.
    acceptor_side: Mapped[Optional[str]] = mapped_column(
        String(8),
        nullable=True,
        comment="Computed and stored when challenge is accepted",
    )
    amount_stars: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Stars each participant must deposit (symmetric)",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="open",
        nullable=False,
        index=True,
        comment="open | locked | resolved | cancelled | expired",
    )
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="True = visible in The Market (anyone can accept)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    winner_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
    )
    platform_fee_stars: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Fee deducted at resolution (0 in Phase 1 free period)",
    )

    # Relationships
    creator: Mapped["User"] = relationship(
        "User",
        foreign_keys=[creator_id],
        back_populates="created_challenges",
    )
    acceptor: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[acceptor_id],
        back_populates="accepted_challenges",
    )
    match: Mapped["Match"] = relationship(
        "Match",
        back_populates="challenges",
    )
    winner: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[winner_id],
        back_populates="won_challenges",
    )
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction",
        back_populates="challenge",
        lazy="select",
    )

    @property
    def total_pot(self) -> int:
        """Total Stars in the pot (both sides)."""
        return self.amount_stars * 2

    def __repr__(self) -> str:
        return (
            f"<Challenge uuid={self.uuid[:8]}... "
            f"status={self.status} amount={self.amount_stars}⭐>"
        )


# ---------------------------------------------------------------------------
# Transaction Model
# ---------------------------------------------------------------------------

class Transaction(Base):
    """
    An immutable financial record of every Stars movement.

    Types:
        deposit    — User deposited Stars into a challenge (escrow)
        withdrawal — Stars paid out to the winner
        fee        — Platform fee deducted at resolution
        refund     — Stars returned to user (cancellation or expiry)

    Note: In Telegram Stars V1, actual fund movement is manual (admin-operated)
    because Telegram does not yet support bot-initiated Star transfers.
    These records serve as the source of truth for what SHOULD happen.
    """
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    challenge_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("challenges.id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="deposit | withdrawal | fee | refund",
    )
    amount_stars: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Positive = Stars flowing in, negative = flowing out",
    )
    telegram_payment_charge_id: Mapped[Optional[str]] = mapped_column(
        String(256),
        nullable=True,
        comment="Telegram's payment charge ID for deposits (for refund eligibility)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )

    # Relationships
    challenge: Mapped["Challenge"] = relationship(
        "Challenge",
        back_populates="transactions",
    )
    user: Mapped["User"] = relationship(
        "User",
        back_populates="transactions",
    )

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.id} type={self.type} "
            f"amount={self.amount_stars}⭐ challenge={self.challenge_id}>"
        )
