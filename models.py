"""
models.py — SIDES Bot SQLAlchemy ORM Models
All database entities for the P2P sports challenge platform.
"""

import uuid
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime,
    ForeignKey, Float, BigInteger
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """A Telegram user who has interacted with the bot."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(64), nullable=True)       # @handle (may be null)
    first_name = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    total_challenges = Column(Integer, default=0)
    total_wins = Column(Integer, default=0)

    # Relationships
    created_challenges = relationship("Challenge", foreign_keys="Challenge.creator_id", back_populates="creator")
    accepted_challenges = relationship("Challenge", foreign_keys="Challenge.acceptor_id", back_populates="acceptor")
    transactions = relationship("Transaction", back_populates="user")

    def __repr__(self) -> str:
        return f"<User telegram_id={self.telegram_id} name={self.first_name}>"


class Match(Base):
    """A sports match fetched from an external API."""
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    external_id = Column(String(64), unique=True, nullable=False, index=True)
    league = Column(String(32), nullable=False)           # "PL", "CL", "NBA", etc.
    home_team = Column(String(128), nullable=False)
    away_team = Column(String(128), nullable=False)
    kickoff_time = Column(DateTime, nullable=False)
    status = Column(String(16), default="scheduled")      # scheduled | live | finished
    home_score = Column(Integer, nullable=True)
    away_score = Column(Integer, nullable=True)
    winner = Column(String(8), default="tbd")             # home | draw | away | tbd
    fetched_at = Column(DateTime, default=datetime.utcnow)

    challenges = relationship("Challenge", back_populates="match")

    def __repr__(self) -> str:
        return f"<Match {self.home_team} vs {self.away_team} @ {self.kickoff_time}>"


class Challenge(Base):
    """A P2P sports challenge between two users."""
    __tablename__ = "challenges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    # Participants
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    acceptor_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Match & sides
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False)
    creator_side = Column(String(8), nullable=False)      # home | draw | away
    # acceptor_side is always the opposite — computed in challenge_service

    # Financials (in Telegram Stars / XTR)
    amount_stars = Column(Integer, nullable=False)        # Each side puts up this amount
    platform_fee_stars = Column(Integer, default=0)       # Fee taken at resolution

    # Status lifecycle: open → locked → resolved | cancelled | expired
    status = Column(String(12), default="open", nullable=False)

    # Visibility: False = private invite-only, True = listed on The Market
    is_public = Column(Boolean, default=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    accepted_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    # Resolution
    winner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    creator_paid = Column(Boolean, default=False)         # Creator has locked Stars
    acceptor_paid = Column(Boolean, default=False)        # Acceptor has locked Stars

    # Relationships
    creator = relationship("User", foreign_keys=[creator_id], back_populates="created_challenges")
    acceptor = relationship("User", foreign_keys=[acceptor_id], back_populates="accepted_challenges")
    match = relationship("Match", back_populates="challenges")
    transactions = relationship("Transaction", back_populates="challenge")

    @property
    def acceptor_side(self) -> str:
        """The acceptor always takes the opposite side to the creator."""
        opposites = {"home": "away", "away": "home", "draw": "draw"}
        # If draw is creator side, acceptor gets either home or away — they choose
        return opposites.get(self.creator_side, "away")

    @property
    def pot_stars(self) -> int:
        return self.amount_stars * 2

    @property
    def invite_link(self) -> str:
        from config import BOT_USERNAME
        return f"https://t.me/{BOT_USERNAME}?start=ref_{self.uuid}"

    def __repr__(self) -> str:
        return f"<Challenge {self.uuid[:8]}... {self.status} {self.amount_stars}⭐>"


class Transaction(Base):
    """Financial transaction record for escrow audit trail."""
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    challenge_id = Column(Integer, ForeignKey("challenges.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    type = Column(String(16), nullable=False)             # deposit | withdrawal | fee | refund
    amount_stars = Column(Integer, nullable=False)
    telegram_payment_charge_id = Column(String(256), nullable=True)  # Telegram's charge ID
    created_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(String(256), nullable=True)

    # Relationships
    challenge = relationship("Challenge", back_populates="transactions")
    user = relationship("User", back_populates="transactions")

    def __repr__(self) -> str:
        return f"<Transaction {self.type} {self.amount_stars}⭐ challenge={self.challenge_id}>"
