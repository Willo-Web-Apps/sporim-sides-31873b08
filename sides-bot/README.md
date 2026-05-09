# SIDES — Peer-to-Peer Sports Challenge Bot

**@sideupbot** · Challenge your friends on real sports matches. Lock equal Stars. Winner takes all — automatically.

> SIDES is a social escrow platform, not a gambling service. We hold funds between two private individuals and automate the transfer when a public sports result is confirmed.

---

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Local Setup](#local-setup)
- [Configuration](#configuration)
- [Running the Bot](#running-the-bot)
- [Bot Commands](#bot-commands)
- [Sports APIs](#sports-apis)
- [Architecture](#architecture)
- [Railway Deployment](#railway-deployment)
- [Telegram Stars Notes](#telegram-stars-notes)
- [Roadmap](#roadmap)

---

## Overview

SIDES lets Telegram users:
1. Pick a sports match (Premier League, NBA, Champions League)
2. Choose a side (Home / Draw / Away)
3. Set a ⭐ Stars amount and send a challenge invite link to a friend
4. Both users lock their Stars — the bot holds them in escrow
5. When the match ends, the bot auto-resolves via sports API and the winner gets the pot (minus 2% platform fee)

Two modes:
- **Challenge a Friend** — private invite link for a specific person
- **The Market** — open list where anyone can accept your challenge

---

## Prerequisites

- **Python 3.12+** — [Download](https://www.python.org/downloads/)
- **pip** (comes with Python)
- **A Telegram bot token** — create one via [@BotFather](https://t.me/BotFather)
- **football-data.org API key** — free at [football-data.org/client/register](https://www.football-data.org/client/register)

---

## Local Setup

```bash
# 1. Clone the repository
git clone https://github.com/your-org/sides-bot.git
cd sides-bot

# 2. Create and activate a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
# .venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Now edit .env with your actual values (see Configuration section)
```

---

## Configuration

Edit `.env` with your values:

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `ADMIN_USER_IDS` | ✅ | Comma-separated Telegram user IDs for admin access |
| `BOT_USERNAME` | ✅ | Bot username without @ (e.g. `sideupbot`) |
| `FOOTBALL_DATA_API_KEY` | ✅ | Free key from football-data.org |
| `DATABASE_URL` | ✅ | SQLite path (default: `sqlite+aiosqlite:///sides.db`) |
| `ENVIRONMENT` | ⬜ | `development` or `production` (default: `development`) |

### Getting a Football-Data.org API Key

1. Visit [football-data.org/client/register](https://www.football-data.org/client/register)
2. Sign up for the free tier (gives you Premier League, Champions League, and more)
3. Copy your API key from the dashboard
4. Paste it into `.env` as `FOOTBALL_DATA_API_KEY`

**Free tier limits:** 10 requests/minute. The bot caches results for 10 minutes to stay well within limits.

---

## Running the Bot

```bash
# Make sure your .env is configured, then:
python main.py
```

You should see:
```
INFO - Database initialized
INFO - SIDES bot starting...
INFO - Result checker scheduled (every 15 minutes)
INFO - Application started, polling for updates...
```

Send `/start` to your bot on Telegram to verify it's working.

---

## Bot Commands

### User Commands

| Command | Description |
|---|---|
| `/start` | Welcome screen with main menu |
| `/start ref_<uuid>` | Accept a specific challenge via invite link |
| `/challenge` | Create a new sports challenge |
| `/market` | Browse open public challenges |
| `/help` | Show all commands and FAQ |

### Admin Commands (restricted to `ADMIN_USER_IDS`)

| Command | Description |
|---|---|
| `/resolve <uuid> <home\|draw\|away>` | Manually resolve a challenge |
| `/stats` | Platform statistics |
| `/pending` | List challenges awaiting resolution |
| `/broadcast <message>` | Send message to all users |

---

## Sports APIs

### Football (football-data.org)
- **Premier League** (`PL`) — Top flight English football
- **Champions League** (`CL`) — UEFA Champions League
- **World Cup** (`WC`) — FIFA World Cup (when active)
- Matches fetched for next 7 days
- Results polled every 15 minutes after kickoff

### NBA (balldontlie.io)
- Free tier, no API key required
- Games fetched for next 7 days
- Results polled every 15 minutes

---

## Architecture

```
sides-bot/
├── main.py                 # Entry point — ApplicationBuilder, scheduler
├── config.py               # Environment variables & constants
├── models.py               # SQLAlchemy ORM models
├── database.py             # Async engine, session management
│
├── handlers/               # Telegram update handlers
│   ├── start.py            # /start command + deep link handling
│   ├── challenge.py        # Challenge creation (ConversationHandler)
│   ├── market.py           # Public challenge market
│   ├── payment.py          # Telegram Stars payment flow
│   ├── admin.py            # Admin-only commands
│   └── help.py             # /help command + FAQ
│
├── services/               # Business logic (no Telegram deps)
│   ├── challenge_service.py # Create, accept, resolve, cancel challenges
│   ├── sports_api.py        # Football-data.org + BallDontLie clients
│   ├── escrow_service.py    # Fund locking, release, refund logic
│   └── result_checker.py   # APScheduler job for auto-resolution
│
└── utils/                  # Shared utilities
    ├── keyboards.py         # InlineKeyboardMarkup builders
    └── formatters.py        # Message formatting helpers
```

---

## Railway Deployment

1. **Push to GitHub** — make sure your code is in a GitHub repository

2. **Create a new Railway project**
   - Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
   - Select your `sides-bot` repository

3. **Add environment variables** in Railway dashboard:
   - `BOT_TOKEN` — your bot token
   - `ADMIN_USER_IDS` — comma-separated admin IDs
   - `BOT_USERNAME` — `sideupbot`
   - `FOOTBALL_DATA_API_KEY` — your API key
   - `DATABASE_URL` — `sqlite+aiosqlite:///sides.db` (or add a PostgreSQL plugin)
   - `ENVIRONMENT` — `production`

4. **Deploy** — Railway auto-deploys on every push to main

5. **Check logs** — Railway dashboard → Deployments → View Logs

The `railway.toml` and `Procfile` are pre-configured for you.

> **Note on SQLite in Railway:** SQLite works for MVP but the file is ephemeral (lost on redeploy). For production, add a Railway PostgreSQL plugin and update `DATABASE_URL` to use `postgresql+asyncpg://...`. You'll also need to add `asyncpg` to `requirements.txt`.

---

## Telegram Stars Notes

Telegram Stars (XTR) is the in-app currency used for payments. Key V1 limitations:

- ✅ **Bot can receive Stars** — via standard invoice flow
- ❌ **Bot cannot send Stars** — Telegram doesn't support bot-initiated Star transfers yet
- **V1 Workaround:** The bot records who won and the payout amount. Admins manually refund via Telegram's refund system or Stars transfers when the feature becomes available.
- Winners are notified of their win and the payout amount. Admin coordinates the actual transfer.

This will be replaced with automatic payouts when Telegram enables bot-to-user Star transfers.

---

## Roadmap

| Version | Target | Features |
|---|---|---|
| **V1** | May 23, 2025 | MVP — Premier League, challenge creation, invite links, escrow, manual resolution, 0% fee |
| **V1.1** | May 28, 2025 | Auto-resolution via API, result share card |
| **V1.2** | June 2025 | NBA support, result auto-resolution |
| **V2** | Summer 2026 | World Cup, all leagues, white-label, 2% commission enabled |

---

## Legal

SIDES is a technology escrow service, not a gambling operator. We do not set odds, take positions, or operate a house. Users challenge each other directly on the outcome of public sports events. Users are responsible for compliance with local laws. See Terms of Service for details.

---

*Built with [python-telegram-bot](https://python-telegram-bot.org/) v21 · [football-data.org](https://www.football-data.org/) · [balldontlie.io](https://www.balldontlie.io/)*
