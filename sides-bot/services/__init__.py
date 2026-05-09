"""
services/ — SIDES Bot Business Logic
======================================
Pure business logic with no Telegram dependencies.
Each module is independently testable.

Modules:
    sports_api        — External sports data clients (football-data.org, BallDontLie)
    challenge_service — Challenge lifecycle management
    escrow_service    — Fund locking, release, and refund tracking
    result_checker    — APScheduler job for automatic match resolution
"""
