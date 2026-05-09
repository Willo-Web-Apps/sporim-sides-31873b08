"""
main.py — SideUp Bot entry point.
Wires up all handlers, initialises the DB, starts the scheduler, and runs polling.
"""

import asyncio
import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from config import BOT_TOKEN
from db import init_db
from handlers.admin import admin_dashboard, refund_all, resolve_manual
from handlers.challenge import build_challenge_conversation
from handlers.market import accept_challenge_callback, show_market
from handlers.payment import pre_checkout_query_handler, successful_payment_handler
from handlers.start import decline_challenge, show_stats, start
from services.scheduler import build_scheduler, set_bot_app

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Called after the Application is fully initialised."""
    await init_db()
    logger.info("✅ Database ready.")

    scheduler = build_scheduler()
    set_bot_app(application)
    scheduler.start()
    logger.info("✅ Scheduler started (result poll every 5 min, expiry every hour).")


def main() -> None:
    logger.info("🚀 Starting SideUp bot (@sideupbot)…")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── Conversation: create a challenge ──────────────────────────────────────
    app.add_handler(build_challenge_conversation())

    # ── Commands ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("market", show_market))
    app.add_handler(CommandHandler("newchallenge", lambda u, c: None))  # handled by conversation
    app.add_handler(CommandHandler("admin", admin_dashboard))
    app.add_handler(CommandHandler("resolve", resolve_manual))
    app.add_handler(CommandHandler("refundall", refund_all))

    # ── Callback queries ──────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(show_stats, pattern="^my_stats$"))
    app.add_handler(CallbackQueryHandler(show_market, pattern=r"^market_page_\d+$"))
    app.add_handler(CallbackQueryHandler(accept_challenge_callback, pattern=r"^accept_"))
    app.add_handler(CallbackQueryHandler(decline_challenge, pattern="^decline_challenge$"))

    # ── Payments ──────────────────────────────────────────────────────────────
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_query_handler))
    app.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler)
    )

    # ── Run ───────────────────────────────────────────────────────────────────
    logger.info("Bot is running — polling for updates.")
    app.run_polling(allowed_updates=["message", "callback_query", "pre_checkout_query"])


if __name__ == "__main__":
    main()
