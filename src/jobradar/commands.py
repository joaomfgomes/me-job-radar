"""Telegram command handlers.

Each handler takes plain data (never a Telegram object) and returns the
reply as an HTML string. Nothing here touches the network -- listener.py is
the only thing that talks to Telegram -- so every handler is tested offline
against a temporary SQLite file.

Replies are sent with parse_mode=HTML, so anything that came from a job
board, from the model, or from the user is passed through escape().
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from html import escape

from jobradar.notify import MAX_REASON_CHARS, _link
from jobradar.store import Store

HELP_TEXT = (
    "<b>job-radar commands</b>\n"
    "/jobs — last 10 jobs recorded\n"
    "/rejected — jobs that were scored but held back, and why\n"
    "/stats [days] — the funnel: how many jobs were seen, filtered and alerted\n"
    "/market [days] — what postings ask for: skills, work mode, seniority, salary\n"
    "/gaps [days] — most requested skills your CV does not show\n"
    "/status — uptime, poll health, jobs per search\n"
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
        dt = datetime.strptime(sql_dt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
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
        url = row["url"] or ""
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


def format_status(store: Store, pipeline, settings, started_at: float, next_run_at: float | None) -> str:
    """Is it alive, and is every search still returning something?"""
    health = pipeline.health_snapshot()
    by_search = store.counts_by_search()
    lines = [
        "<b>job-radar status</b>",
        f"Up since: {_relative_age(started_at)}",
        f"Last poll: {_relative_age(health['last_poll_at'])}",
        f"Last successful fetch: {_relative_age(health['last_success_at'])}",
        f"Next poll: {_relative_future(next_run_at)}",
        f"Jobs recorded: {store.total_count()}",
        "",
        "<b>Searches</b>",
    ]
    for search in settings.searches:
        errors = health["consecutive_errors"].get(search.name, 0)
        empty = health["consecutive_empty"].get(search.name, 0)
        flag = ""
        if errors:
            flag = f" ⚠️ {errors} failed fetches in a row"
        elif empty:
            flag = f" ({empty} empty cycles in a row)"
        lines.append(f"• {escape(search.name)}: {by_search.get(search.name, 0)} jobs{flag}")
    return "\n".join(lines)


DEFAULT_PERIOD_DAYS = 30
MAX_PERIOD_DAYS = 365

# evaluation.Outcome value -> how /stats words it. Ordered as the cascade
# runs, so the list reads top to bottom like the funnel itself.
OUTCOME_LABELS = {
    "prefilter": "Prefilter (title/employer rules)",
    "title_low": "Title not relevant",
    "language": "Language",
    "years": "Too many years required",
    "not_junior": "Too senior",
    "location": "Location does not fit",
    "low_coverage": "Low CV coverage",
    "alerted": "✅ Alerted",
    "alerted_unfiltered": "⚠️ Alerted unfiltered",
    "repost": "Repost of a job already evaluated",
    "not_evaluated": "Seeded (first run, not evaluated)",
}


def _parse_days(argument: str) -> int:
    """'/stats 7' -> 7. Anything missing or unusable falls back to the
    default rather than answering with an error."""
    try:
        days = int(argument.split()[0])
    except (IndexError, ValueError):
        return DEFAULT_PERIOD_DAYS
    return max(1, min(days, MAX_PERIOD_DAYS))


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "0%"


def format_stats(store: Store, argument: str = "") -> str:
    """The funnel for a period: where every posting's evaluation ended."""
    days = _parse_days(argument)
    counts = store.outcome_counts(days)
    total = sum(counts.values())
    if not total:
        return f"No jobs recorded in the last {days} day(s)."

    evaluated = total - counts.get("not_evaluated", 0)
    lines = [f"<b>Last {days} day(s)</b> — {total} jobs seen, {evaluated} evaluated", ""]
    for outcome, label in OUTCOME_LABELS.items():
        count = counts.get(outcome, 0)
        if count:
            share = f" ({_pct(count, evaluated)})" if outcome != "not_evaluated" else ""
            lines.append(f"{label}: <b>{count}</b>{share}")

    averages = store.match_averages(days)
    if averages["scored"]:
        lines += ["", f"<b>Read in full:</b> {averages['scored']} postings"]
        lines.append(f"Average CV coverage: {averages['avg_coverage']:.0f}%")
        if averages["avg_chance"] is not None:
            lines.append(f"Average interview chance (estimate): {averages['avg_chance']:.0f}%")

    best = store.best_matches(days)
    if best:
        lines += ["", "<b>Best chances</b>"]
        for row in best:
            lines.append(
                f'• ~{row["interview_chance_pct"]}% · {row["coverage_pct"]}% match — '
                f'{_link(row["url"] or "", escape(row["title"]))} ({escape(row["company"])})'
            )

    if counts.get("alerted_unfiltered", 0) > counts.get("alerted", 0):
        lines += ["", "⚠️ Most alerts went out unfiltered — scoring was unavailable. Check the log."]
    return "\n".join(lines)


def _skill_line(row: dict, total: int) -> str:
    mark = "✅" if row["candidate_has"] else "❌"
    return (
        f'{mark} {escape(row["name"])} — {row["jobs"]} jobs ({_pct(row["jobs"], total)}), '
        f'required in {row["required_in"]}'
    )


def _distribution(pairs: list[tuple[str, int]]) -> str:
    return ", ".join(f"{escape(str(value))} {count}" for value, count in pairs)


def format_market(store: Store, argument: str = "") -> str:
    """What the postings read in full are asking for."""
    days = _parse_days(argument)
    total = store.insights_count(days)
    if not total:
        return (
            f"No market data for the last {days} day(s) yet. It is collected each time "
            "a job description is read in full, so it needs scoring to be working."
        )

    lines = [f"<b>Market, last {days} day(s)</b> — based on {total} postings read in full", ""]
    lines.append("<b>Most requested skills</b> (✅ on your CV, ❌ not)")
    lines += [_skill_line(row, total) for row in store.skill_demand(days)]
    lines += [
        "",
        f"<b>Work mode:</b> {_distribution(store.insight_distribution('work_mode', days))}",
        f"<b>Seniority:</b> {_distribution(store.insight_distribution('seniority_level', days))}",
        f"<b>Roles:</b> {_distribution(store.insight_distribution('role_family', days))}",
    ]
    for row in store.salary_summary(days):
        low = f"{row['avg_min']:,.0f}" if row["avg_min"] is not None else "?"
        high = f"{row['avg_max']:,.0f}" if row["avg_max"] is not None else "?"
        lines.append(
            f"<b>Salary ({escape(row['currency'])}):</b> {low}–{high} per year on average, "
            f"stated by {row['jobs']} posting(s)"
        )
    lines += ["", "Only relevant-looking roles are read in full, so this describes your target market, not all jobs."]
    return "\n".join(lines)


def format_gaps(store: Store, argument: str = "") -> str:
    """The most requested skills the CV does not show -- what to learn, or
    what to add to the CV if you already know it."""
    days = _parse_days(argument)
    total = store.insights_count(days)
    rows = store.skill_demand(days, limit=20, only_missing=True)
    if not total or not rows:
        return f"No skill gaps found in the last {days} day(s) (based on {total} postings)."

    lines = [f"<b>Skill gaps, last {days} day(s)</b> — from {total} postings read in full", ""]
    for row in rows:
        lines.append(
            f'• <b>{escape(row["name"])}</b> ({escape(row["category"])}) — {row["jobs"]} jobs '
            f'({_pct(row["jobs"], total)}), required in {row["required_in"]}'
        )
    lines += ["", "If you do have one of these, add it to cv.pdf and re-run cv_ingest — the model only credits what the CV shows."]
    return "\n".join(lines)


def format_help() -> str:
    return HELP_TEXT


def dispatch(text: str, *, store: Store, pipeline, settings, started_at: float, next_run_at: float | None) -> str:
    """Route a raw message to its handler. Unknown input gets a nudge, not silence."""
    command, _, argument = text.strip().partition(" ")
    command = command.lower().split("@")[0]  # strip the "/jobs@bot_name" group-chat form
    argument = argument.strip()

    if command == "/jobs":
        return format_jobs(store)
    if command == "/rejected":
        return format_rejected(store)
    if command == "/stats":
        return format_stats(store, argument)
    if command == "/market":
        return format_market(store, argument)
    if command == "/gaps":
        return format_gaps(store, argument)
    if command == "/status":
        return format_status(store, pipeline, settings, started_at, next_run_at)
    if command in ("/help", "/start"):
        return format_help()
    return "Unknown command. Try /help."
