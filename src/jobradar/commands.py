"""Telegram command handlers.

Each handler takes plain data (never a Telegram object) and returns a
formatted string. No network calls happen in here -- the listener loop
(added in a later step) is the only thing that talks to Telegram, which
means every handler is testable offline against a temp SQLite file.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from html import escape

from jobradar.store import Store, url_for_job_id

HELP_TEXT = (
    "<b>job-radar commands</b>\n"
    "/jobs — last 10 jobs recorded\n"
    "/status — uptime, poll health, DB counts\n"
    "/help — this message\n\n"
    "New matches are pushed here automatically as they're found — "
    "these commands are just for checking in."
)


def _relative_age(timestamp: float | None) -> str:
    """Format a unix timestamp (or None) as '5m ago' / 'never'."""
    if timestamp is None:
        return "never"
    seconds = max(0, time.time() - timestamp)
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _relative_future(timestamp: float | None) -> str:
    """Format a future unix timestamp (or None) as 'in 5m'."""
    if timestamp is None:
        return "unknown"
    seconds = timestamp - time.time()
    if seconds <= 0:
        return "any moment now"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "in under a minute"
    if minutes < 60:
        return f"in {minutes}m"
    hours = minutes // 60
    return f"in {hours}h {minutes % 60}m"


def _relative_age_from_sql_datetime(sql_dt: str) -> str:
    """SQLite's datetime('now') stores UTC text like '2026-08-14 15:17:49'."""
    try:
        dt = datetime.strptime(sql_dt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return sql_dt
    return _relative_age(dt.timestamp())


def format_jobs(store: Store, limit: int = 10) -> str:
    rows = store.recent_jobs(limit=limit)
    if not rows:
        return "No jobs recorded yet -- the first poll cycle hasn't found anything to seed with."

    lines = [f"<b>Last {len(rows)} job(s) seen</b>"]
    for i, row in enumerate(rows, start=1):
        age = _relative_age_from_sql_datetime(row["first_seen"])
        url = url_for_job_id(row["job_id"])
        lines.append(
            f'{i}. <b>{escape(row["title"])}</b> — {escape(row["company"])} '
            f'({escape(row["search_name"])}, {age})\n'
            f'   <a href="{url}">Open on LinkedIn</a>'
        )
    return "\n".join(lines)


def format_status(store: Store, pipeline, settings, started_at: float, next_run_at: float | None) -> str:
    health = pipeline.health_snapshot()
    uptime = _relative_age(started_at).replace(" ago", "")
    if uptime in ("just now",):
        uptime = "under a minute"

    total = store.total_count()
    by_search = store.counts_by_search()

    lines = [
        "<b>job-radar status</b>",
        f"Uptime: {uptime}",
        f"Last poll attempt: {_relative_age(health['last_poll_at'])}",
        f"Last successful fetch: {_relative_age(health['last_success_at'])}",
        f"Next scheduled poll: {_relative_future(next_run_at)}",
        f"Total jobs recorded: {total}",
        "",
        "<b>Searches:</b>",
    ]
    for search in settings.searches:
        count = by_search.get(search.name, 0)
        errors = health["consecutive_errors"].get(search.name, 0)
        empty = health["consecutive_empty"].get(search.name, 0)
        flag = ""
        if errors:
            flag = f" ⚠️ {errors} consecutive fetch errors"
        elif empty:
            flag = f" ({empty} consecutive empty cycles)"
        lines.append(f"• {escape(search.name)}: {count} jobs{flag}")

    return "\n".join(lines)


def format_help() -> str:
    return HELP_TEXT


def dispatch(text: str, *, store: Store, pipeline, settings, started_at: float, next_run_at: float | None) -> str:
    """Route a raw command string to a handler. Unknown input gets a nudge, not silence."""
    command = text.strip().split()[0].lower() if text.strip() else ""
    command = command.split("@")[0]  # strip "/jobs@your_bot_name" group-chat form

    if command == "/jobs":
        return format_jobs(store)
    if command == "/status":
        return format_status(store, pipeline, settings, started_at, next_run_at)
    if command in ("/help", "/start"):
        return format_help()
    return "Unknown command. Try /help."
