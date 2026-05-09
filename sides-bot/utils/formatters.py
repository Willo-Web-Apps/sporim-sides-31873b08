"""
utils/formatters.py — SIDES Bot Message Formatters
====================================================
All user-facing message text is generated here.
Keep formatting logic out of handlers — handlers call these functions.

Follows Telegram MarkdownV2 syntax where applicable.
All functions return plain str (use ParseMode.HTML or MARKDOWN_V2 as needed).

NOTE: We use HTML parse mode throughout the bot for simplicity.
      Wrap bold in <b>text</b>, italic in <i>text</i>, code in <code>text</code>.
"""

import math
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Side helpers
# ---------------------------------------------------------------------------

SIDE_EMOJI: dict[str, str] = {
    "home": "🏠",
    "away": "✈️",
    "draw": "🤝",
    "tbd": "⏳",
}

SIDE_LABEL: dict[str, str] = {
    "home": "Home",
    "away": "Away",
    "draw": "Draw",
}

STATUS_EMOJI: dict[str, str] = {
    "open": "🟡",
    "locked": "🔒",
    "resolved": "✅",
    "cancelled": "❌",
    "expired": "⌛",
}


def _side_str(side: str, team_name: str | None = None) -> str:
    """Human-readable side string with emoji."""
    emoji = SIDE_EMOJI.get(side, "❓")
    label = SIDE_LABEL.get(side, side.title())
    if team_name:
        return f"{emoji} {team_name} ({label})"
    return f"{emoji} {label}"


def _time_until(dt: datetime) -> str:
    """
    Return a human-readable string for time until a future datetime.
    e.g. "in 2 days", "in 4 hours", "in 30 minutes", "started"
    """
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    delta = dt - now
    total_seconds = int(delta.total_seconds())

    if total_seconds < 0:
        return "started"
    elif total_seconds < 3600:
        mins = total_seconds // 60
        return f"in {mins}m"
    elif total_seconds < 86400:
        hours = total_seconds // 3600
        return f"in {hours}h"
    else:
        days = total_seconds // 86400
        return f"in {days}d"


# ---------------------------------------------------------------------------
# Match formatting
# ---------------------------------------------------------------------------

def format_match(match) -> str:
    """
    Single-line match summary.
    e.g. "Arsenal vs Chelsea · Sat 3 May · 18:30 UTC"

    Args:
        match: Match ORM object or dict with home_team, away_team, kickoff_time
    """
    if hasattr(match, "home_team"):
        home = match.home_team
        away = match.away_team
        kickoff: datetime = match.kickoff_time
    else:
        home = match["home_team"]
        away = match["away_team"]
        kickoff = match["kickoff_time"]

    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)

    day_str = kickoff.strftime("%-d %b")   # "3 May"
    day_name = kickoff.strftime("%a")       # "Sat"
    time_str = kickoff.strftime("%H:%M")    # "18:30"

    return f"{home} vs {away} · {day_name} {day_str} · {time_str} UTC"


def format_kickoff_display(kickoff: datetime) -> str:
    """Short display for match buttons: 'Sat 18:30'"""
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return kickoff.strftime("%a %H:%M")


# ---------------------------------------------------------------------------
# Challenge card
# ---------------------------------------------------------------------------

def format_challenge_card(challenge, match) -> str:
    """
    Rich challenge summary card for sharing or viewing.

    Args:
        challenge: Challenge ORM object
        match:     Match ORM object

    Returns:
        HTML-formatted string ready for Telegram message
    """
    status_emoji = STATUS_EMOJI.get(challenge.status, "❓")
    creator_name = challenge.creator.display_name() if challenge.creator else "Unknown"

    creator_side_team = (
        match.home_team if challenge.creator_side == "home"
        else match.away_team if challenge.creator_side == "away"
        else None
    )

    lines = [
        f"⚡️ <b>SIDES Challenge</b> {status_emoji}",
        "",
        f"🏟 <b>{match.home_team}</b>  vs  <b>{match.away_team}</b>",
        f"🗓  {format_match(match)}",
        f"🏆  {match.league.upper()}",
        "",
        f"👤 <b>Creator:</b> {creator_name}",
        f"   Backing: {_side_str(challenge.creator_side, creator_side_team)}",
        "",
        f"💰 <b>Each side puts up:</b> {challenge.amount_stars:,} ⭐",
        f"🏆 <b>Total pot:</b> {challenge.total_pot:,} ⭐",
        "",
        f"⏳ Kicks off: {_time_until(match.kickoff_time)}",
    ]

    if challenge.status == "open":
        lines += [
            "",
            "👇 <i>Accept to play the opposite side!</i>",
        ]
    elif challenge.status == "locked":
        acceptor_name = challenge.acceptor.display_name() if challenge.acceptor else "?"
        acceptor_side_team = (
            match.home_team if challenge.acceptor_side == "home"
            else match.away_team if challenge.acceptor_side == "away"
            else None
        )
        lines += [
            "",
            f"🤺 <b>Challenger:</b> {acceptor_name}",
            f"   Backing: {_side_str(challenge.acceptor_side or 'tbd', acceptor_side_team)}",
            "",
            "🔒 <i>Stakes locked — may the best pick win!</i>",
        ]
    elif challenge.status == "resolved":
        winner_name = challenge.winner.display_name() if challenge.winner else "Unknown"
        lines += [
            "",
            f"🏆 <b>Winner:</b> {winner_name}",
            f"💸 Payout: {challenge.total_pot - challenge.platform_fee_stars:,} ⭐",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Market listing
# ---------------------------------------------------------------------------

def format_market_listing(challenges: list) -> str:
    """
    Header message for The Market screen.

    Args:
        challenges: List of open Challenge objects (current page)
    """
    if not challenges:
        return (
            "📋 <b>The Market</b>\n\n"
            "No open challenges right now.\n\n"
            "Be the first! Create a challenge and post it here — "
            "anyone can accept. 🚀"
        )

    count = len(challenges)
    lines = [
        "📋 <b>The Market</b>",
        f"<i>{count} open challenge{'s' if count != 1 else ''} waiting for a challenger</i>",
        "",
    ]

    for i, challenge in enumerate(challenges, 1):
        match = challenge.match
        creator_name = challenge.creator.display_name() if challenge.creator else "?"
        side_emoji = SIDE_EMOJI.get(challenge.creator_side, "❓")
        side_team = (
            match.home_team if challenge.creator_side == "home"
            else match.away_team if challenge.creator_side == "away"
            else "Draw"
        )

        lines.append(
            f"{i}. <b>{match.home_team} vs {match.away_team}</b>\n"
            f"   {side_emoji} {creator_name} backs <b>{side_team}</b> "
            f"· {challenge.amount_stars:,}⭐ · {_time_until(match.kickoff_time)}"
        )

    lines += [
        "",
        "👇 <i>Tap Accept to take the opposite side!</i>",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stats (admin)
# ---------------------------------------------------------------------------

def format_stats(stats: dict) -> str:
    """
    Admin platform statistics dashboard.

    Expected keys in stats dict:
        total_users, total_challenges, open_challenges, locked_challenges,
        resolved_challenges, cancelled_challenges, total_volume_stars,
        total_fees_stars
    """
    return (
        "📊 <b>SIDES Platform Stats</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total users:          <b>{stats.get('total_users', 0):,}</b>\n"
        f"⚡ Total challenges:     <b>{stats.get('total_challenges', 0):,}</b>\n"
        f"\n"
        f"🟡 Open:                 <b>{stats.get('open_challenges', 0):,}</b>\n"
        f"🔒 Locked (in escrow):   <b>{stats.get('locked_challenges', 0):,}</b>\n"
        f"✅ Resolved:             <b>{stats.get('resolved_challenges', 0):,}</b>\n"
        f"❌ Cancelled/Expired:    <b>{stats.get('cancelled_challenges', 0):,}</b>\n"
        f"\n"
        f"⭐ Total volume:         <b>{stats.get('total_volume_stars', 0):,} Stars</b>\n"
        f"💰 Platform fees earned: <b>{stats.get('total_fees_stars', 0):,} Stars</b>\n"
    )


def format_pending_challenges(challenges: list) -> str:
    """
    Admin listing of locked challenges awaiting resolution.

    Args:
        challenges: List of locked Challenge objects
    """
    if not challenges:
        return "✅ No challenges pending resolution."

    lines = [
        f"⏳ <b>Pending Resolution</b> ({len(challenges)} challenges)",
        "",
    ]

    for challenge in challenges:
        match = challenge.match
        creator = challenge.creator.display_name() if challenge.creator else "?"
        acceptor = challenge.acceptor.display_name() if challenge.acceptor else "?"
        lines.append(
            f"• <code>{challenge.uuid}</code>\n"
            f"  {match.home_team} vs {match.away_team}\n"
            f"  {creator} vs {acceptor} · {challenge.amount_stars}⭐ each\n"
            f"  Kicked off: {_time_until(match.kickoff_time)}"
        )
        lines.append("")

    lines.append("Use: /resolve &lt;uuid&gt; &lt;home|draw|away&gt;")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Welcome / How It Works
# ---------------------------------------------------------------------------

def format_welcome(first_name: str) -> str:
    """Welcome message shown on /start."""
    return (
        f"👋 Hey {first_name}!\n\n"
        "⚡ <b>Welcome to SIDES</b>\n\n"
        "Challenge your friends on real sports matches.\n"
        "Lock equal ⭐ Stars. Winner takes all — automatically.\n\n"
        "No bookmaker. No house edge. Just you and them.\n\n"
        "What would you like to do?"
    )


def format_how_it_works() -> str:
    """Explainer message for How It Works."""
    return (
        "ℹ️ <b>How SIDES Works</b>\n\n"
        "<b>1. Pick a match</b> 🏆\n"
        "Choose from upcoming Premier League, Champions League, or NBA games.\n\n"
        "<b>2. Pick your side</b> ⚡\n"
        "Back the home team, away team, or a draw. Set your Stars amount.\n\n"
        "<b>3. Challenge a friend</b> 📤\n"
        "Send the invite link — your friend accepts and locks their Stars too.\n\n"
        "<b>4. Winner takes all</b> 🏆\n"
        "When the match ends, we check the result automatically. "
        "The winner gets the full pot. Simple.\n\n"
        "━━━━━━━━━━━━━━\n"
        "🔒 <b>Your Stars are safe</b>\n"
        "SIDES is a technology escrow service, not a gambling platform. "
        "We hold funds between two private individuals and release them "
        "when a public sports result is confirmed.\n\n"
        "💸 <b>Platform fee:</b> 0% right now (free launch period!) · 2% later\n\n"
        "<i>We are NOT a bookmaker. We don't set odds. We don't take risk.</i>"
    )


def format_payment_confirmation(
    challenge,
    match,
    user_display_name: str,
    is_creator: bool,
) -> str:
    """
    Message sent to both parties after both sides have paid.
    """
    your_side = challenge.creator_side if is_creator else (challenge.acceptor_side or "tbd")
    side_team = (
        match.home_team if your_side == "home"
        else match.away_team if your_side == "away"
        else "Draw"
    )

    return (
        f"🔒 <b>Stakes locked! Good luck! 🤞</b>\n\n"
        f"<b>{match.home_team} vs {match.away_team}</b>\n"
        f"{format_match(match)}\n\n"
        f"You're backing: {_side_str(your_side, side_team)}\n"
        f"Your stake: {challenge.amount_stars:,} ⭐\n"
        f"Total pot: {challenge.total_pot:,} ⭐\n\n"
        f"I'll check the result automatically after the match ends. "
        f"Winner gets the pot! 🏆\n\n"
        f"<i>Match ID: <code>{challenge.uuid[:8]}</code></i>"
    )
