"""
main.py — SideUp Bot Entry Point
==================================
Bootstraps the full application:

    1. Configures logging
    2. Initialises the SQLite/PostgreSQL database (creates tables)
    3. Builds the PTB Application with all handlers registered
    4. Starts the APScheduler background jobs
    5. Runs in polling mode (no webhook required for early-stage deployment)

Environment:
    Set all required variables in .env (copy from .env.example).
    Required: BOT_TOKEN
    Optional: ADMIN_USER_IDS, DATABASE_URL, FOOTBALL_DATA_API_KEY, etc.

Deployment:
    Railway / Replit: set env vars in the dashboard, Procfile runs "python main.py"
    Local:            python main.py (in the sides-bot/ directory)

Logging:
    Structured to stdout with timestamps.
    In production, pipe to a log aggregator (e.g. Railway's log drain).
"""

import asyncio
import logging
import sys

from telegram import BotCommand
from telegram.ext import Application

import config
from db import close_db, init_db
from handlers import admin, challenge, market, payment, start
from services.scheduler import create_scheduler, set_application
from services import sports_api as sports_api_service

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    """
    Set up structured logging to stdout.

    In development: DEBUG level with verbose APScheduler output muted.
    In production:  INFO level.
    """
    level = logging.DEBUG if not config.IS_PRODUCTION else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # Reduce noise from verbose third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bot command menu
# ---------------------------------------------------------------------------


async def _set_bot_commands(app: Application) -> None:
    """
    Register the bot's command list shown in Telegram's / menu.

    These appear when a user types "/" in a chat with the bot.

    Args:
        app: The running PTB Application.
    """
    commands = [
        BotCommand("start", "Welcome screen & main menu"),
        BotCommand("newchallenge", "Create a new sports challenge"),
        BotCommand("market", "Browse open public challenges"),
        BotCommand("cancel", "Cancel current action"),
    ]
    await app.bot.set_my_commands(commands)
    logger.info("Bot commands set: %s", [c.command for c in commands])


# ---------------------------------------------------------------------------
# Application builder
# ---------------------------------------------------------------------------


def build_application() -> Application:
    """
    Construct the PTB Application with all handlers registered.

    Handler registration order matters for conflict resolution:
        1. ConversationHandlers (challenge creation) — must be first to avoid
           command handlers intercepting mid-conversation messages
        2. Payment callbacks
        3. Market handlers
        4. Start handlers (includes fallback callbacks)
        5. Admin commands

    Returns:
        Configured Application instance (not yet running).
    """
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .build()
    )

    # 1. Challenge creation wizard (ConversationHandler)
    challenge.register(app)

    # 2. Payment flow (Stars invoices, pre-checkout, successful_payment)
    payment.register(app)

    # 3. Market listing
    market.register(app)

    # 4. Start command + deep links + FAQ callbacks
    start.register(app)

    # 5. Admin commands
    admin.register(app)

    logger.info("All handlers registered on Application.")
    return app


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    """
    Full application startup sequence:

        1. Configure logging
        2. Log startup banner
        3. Initialise database tables
        4. Build PTB Application
        5. Set bot commands (nice UX)
        6. Start APScheduler background jobs
        7. Run polling loop (blocks until KeyboardInterrupt / SIGTERM)
        8. Clean shutdown (scheduler, DB, HTTP clients)
    """
    _configure_logging()

    logger.info("=" * 60)
    logger.info("SideUp Bot starting up")
    logger.info("Environment : %s", config.ENVIRONMENT)
    logger.info("Database    : %s", config.DATABASE_URL.split("///")[0])
    logger.info("Bot username: @%s", config.BOT_USERNAME)
    logger.info(
        "Admins      : %s",
        config.ADMIN_USER_IDS if config.ADMIN_USER_IDS else "NONE — set ADMIN_USER_IDS!",
    )
    logger.info("=" * 60)

    # Step 1: Initialise database
    logger.info("Initialising database…")
    await init_db()

    # Step 2: Build application
    app = build_application()

    # Step 3: Set bot command menu (requires bot connection)
    async with app:
        await _set_bot_commands(app)

        # Step 4: Inject app into scheduler (for sending Telegram messages)
        set_application(app)

        # Step 5: Start the APScheduler
        scheduler = create_scheduler()
        scheduler.start()
        logger.info("Background scheduler started.")

        # Step 6: Run polling (blocks here until shutdown signal)
        logger.info("Starting polling loop… (Ctrl+C to stop)")
        try:
            await app.updater.start_polling(
                allowed_updates=["message", "callback_query", "pre_checkout_query"],
                drop_pending_updates=True,   # Ignore updates queued during downtime
            )
            await app.start()

            # Keep running until interrupted
            await asyncio.Event().wait()

        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received.")

        finally:
            # Step 7: Graceful shutdown
            logger.info("Shutting down…")
            scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped.")

            # Close sports API HTTP clients
            await sports_api_service.close_all_clients()
            logger.info("Sports API clients closed.")

            # Close DB connections
            await close_db()
            logger.info("Database connections closed.")

            logger.info("SideUp Bot shutdown complete. Goodbye.")


# ---------------------------------------------------------------------------
# Script entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
