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

from jobradar.notify import MAX_REASON_CHARS, _link
from jobradar.store import Store, url_for_job_id

HELP_TEXT = (
    "<b>job-radar commands</b>\n"
    "/jobs — last 10 jobs recorded\n"
    "/rejected — jobs that were scored but held back, and why\n"
    "/countries — list locations and whether each is active\n"
    "/countries on|off &lt;name&gt; — start or stop searching a location\n"
    "/countries add &lt;name&gt; — validate and start searching a new location\n"
    "/countries remove &lt;name&gt; — drop a location added via /countries add\n"
    "/status — uptime, poll health, DB counts\n"
    "/help — this message\n\n"
    "Matches above the coverage threshold are pushed here automatically — "
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
        # Stored URL, not one rebuilt from job_id: ids are namespaced per
        # board now, and not every board is LinkedIn.
        url = row["url"] or url_for_job_id(row["job_id"].split(":", 1)[-1])
        lines.append(
            f'{i}. <b>{escape(row["title"])}</b> — {escape(row["company"])} '
            f'({escape(row["search_name"])}, {age})\n'
            f'   {_link(url, "Open posting")}'
        )
    return "\n".join(lines)


def format_rejected(store: Store, limit: int = 15) -> str:
    """The audit trail for the coverage threshold: what got held back, and
    why. Without this, aggressive filtering is indistinguishable from a
    broken pipeline."""
    rows = store.rejected_jobs(limit=limit)
    if not rows:
        return "Nothing has been held back yet."

    lines = [f"<b>Last {len(rows)} job(s) held back</b>"]
    for i, row in enumerate(rows, start=1):
        coverage = f'{row["coverage_pct"]}%' if row["coverage_pct"] is not None else "not scored"
        url = row["url"] or ""
        title = escape(row["title"])
        linked = _link(url, title)
        lines.append(
            f'{i}. <b>{linked}</b> — {escape(row["company"])} [{coverage}]\n'
            f'   <i>{escape((row["score_reason"] or "")[:MAX_REASON_CHARS])}</i>'
        )
    return "\n".join(lines)


def _location_names(pipeline) -> list[str]:
    """Rotation order, de-duplicated -- the same location appears once per
    keyword and per source in the expanded searches. Pulled from the
    pipeline (not settings) since matrix-derived searches -- including
    anything added via /countries add -- are computed fresh each cycle,
    not baked into Settings at startup."""
    core, secondary = pipeline.current_searches()
    names: list[str] = []
    for search in core + secondary:
        if search.location_name and search.location_name not in names:
            names.append(search.location_name)
    return names


def format_countries(store: Store, pipeline, argument: str = "") -> str:
    """`/countries` lists; `/countries on|off <name>` toggles a location
    from config.yaml (or a previously-added one); `/countries add <name>`
    validates a new one live against every configured board before
    persisting it (see location_validation.py -- neither board rejects an
    unrecognized location outright, so this can't just trust a 200 OK);
    `/countries remove <name>` drops one that was added this way (a
    config.yaml location can only be paused with /off, not removed here --
    that file is meant to be hand-edited, not overwritten by a bot)."""
    parts = argument.split(maxsplit=1)
    if not parts:
        names = _location_names(pipeline)
        if not names:
            return "No locations configured (config.yaml has no search_matrix)."
        prefs = store.location_prefs()
        custom = store.custom_locations()
        lines = ["<b>Locations</b>"]
        for name in names:
            enabled = prefs.get(name, True)
            tag = " (added)" if name in custom else ""
            lines.append(f'{"✅" if enabled else "⏸"} {escape(name)}{tag}')
        lines.append("")
        lines.append(
            "on|off &lt;name&gt; toggles · add &lt;name&gt; searches a new place · "
            "remove &lt;name&gt; drops an added one"
        )
        return "\n".join(lines)

    action = parts[0].lower()

    if action == "add":
        if len(parts) < 2:
            return "Which location? e.g. /countries add Lisbon"
        return pipeline.validate_and_add_location(parts[1].strip())

    if action == "remove":
        if len(parts) < 2:
            return "Which location? Try /countries to see the names."
        wanted = parts[1].strip()
        removed = store.remove_custom_location(wanted)
        if not removed:
            # Might still be a real, known (config.yaml) location -- just
            # not one this command is allowed to delete.
            if wanted.lower() in (n.lower() for n in _location_names(pipeline)):
                return f"'{escape(wanted)}' is defined in config.yaml -- use /countries off to pause it instead."
            return f"'{escape(wanted)}' wasn't added via /countries add."
        return f"Removed '{escape(wanted)}'."

    if action in ("on", "off"):
        if len(parts) < 2:
            return "Which location? Try /countries to see the names."
        names = _location_names(pipeline)
        wanted = parts[1].strip().lower()
        match = next((n for n in names if n.lower() == wanted), None)
        if match is None:
            return f"Unknown location {escape(parts[1])!r}. Known: {escape(', '.join(names))}"
        store.set_location_enabled(match, action == "on")
        verb = "now being searched" if action == "on" else "paused"
        return f"<b>{escape(match)}</b> is {verb}. Takes effect on the next poll cycle."

    return "Usage: /countries, or /countries on|off|add|remove &lt;name&gt;"


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
        "<b>Core searches (every cycle):</b>",
    ]

    def _search_line(search) -> str:
        count = by_search.get(search.name, 0)
        errors = health["consecutive_errors"].get(search.name, 0)
        empty = health["consecutive_empty"].get(search.name, 0)
        flag = ""
        if errors:
            flag = f" ⚠️ {errors} consecutive fetch errors"
        elif empty:
            flag = f" ({empty} consecutive empty cycles)"
        return f"• {escape(search.name)}: {count} jobs{flag}"

    core, secondary = pipeline.current_searches()

    for search in core:
        lines.append(_search_line(search))

    if secondary:
        pool_size = len(secondary)
        batch = settings.secondary_batch_size
        cycles_per_rotation = -(-pool_size // batch)  # ceil
        lines.append("")
        lines.append(
            f"<b>Secondary searches ({batch}/cycle, full rotation ~{cycles_per_rotation} cycles):</b>"
        )
        for search in secondary:
            lines.append(_search_line(search))

    return "\n".join(lines)


def format_help() -> str:
    return HELP_TEXT


def dispatch(text: str, *, store: Store, pipeline, settings, started_at: float, next_run_at: float | None) -> str:
    """Route a raw command string to a handler. Unknown input gets a nudge, not silence."""
    command = text.strip().split()[0].lower() if text.strip() else ""
    command = command.split("@")[0]  # strip "/jobs@your_bot_name" group-chat form

    if command == "/jobs":
        return format_jobs(store)
    if command == "/rejected":
        return format_rejected(store)
    if command == "/countries":
        argument = text.strip().split(maxsplit=1)
        return format_countries(store, pipeline, argument[1] if len(argument) > 1 else "")
    if command == "/status":
        return format_status(store, pipeline, settings, started_at, next_run_at)
    if command in ("/help", "/start"):
        return format_help()
    return "Unknown command. Try /help."
