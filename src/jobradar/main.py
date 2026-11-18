"""Entry point: builds the objects, schedules the work, and runs until stopped.

    uv run python -m jobradar.main

This is the only module that knows how everything fits together (the
"composition root"): every other module receives what it needs as
arguments, which is what lets the tests swap in fakes.

Two threads:
  * a scheduler thread runs the poll cycle every few minutes and the digest
    once a day;
  * the main thread long-polls Telegram for commands.
"""

from __future__ import annotations

import datetime
import logging
import os
import signal
import time
from html import escape
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jobradar.config import Settings, load_settings
from jobradar.evaluation import Evaluator
from jobradar.listener import CommandListener
from jobradar.llm import API_KEY_ENV
from jobradar.logging_setup import setup_logging
from jobradar.notify import TelegramNotifier
from jobradar.pipeline import Pipeline
from jobradar.profile import Profile, load_profile
from jobradar.scoring import JobScorer
from jobradar.sources import SOURCES
from jobradar.sources.linkedin_jd import JobDescriptionFetcher
from jobradar.store import Store

log = logging.getLogger("jobradar")


def _raise_system_exit(signum, _frame) -> None:
    """launchd/systemd stop a service with SIGTERM, which by default kills
    Python without unwinding. Turning it into SystemExit lets main()'s
    finally block close the HTTP clients and the database. Further SIGTERMs
    are ignored from then on: a service manager signals the whole process
    group, and a second one arriving mid-cleanup would interrupt it."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(0)


def load_scoring(settings: Settings) -> tuple[Profile | None, JobScorer | None]:
    """Filtering degrades in two steps rather than failing to start:

      * no profile.yaml       -> nothing is filtered, every job is alerted;
      * profile, but no key   -> only the deterministic prefilter runs.

    With both, the full cascade in evaluation.py decides what gets through.
    """
    profile_path = Path(settings.profile_path)
    if not profile_path.exists():
        log.warning("No %s found -- every job will be alerted unfiltered (see profile.example.yaml).", profile_path)
        return None, None
    profile = load_profile(profile_path)

    try:
        scorer = JobScorer.from_env(title_model=settings.title_model, deep_model=settings.deep_model)
    except RuntimeError as exc:
        log.warning("Scoring disabled, prefilter only: %s", exc)
        return profile, None

    log.info("Scoring enabled: title model=%s, deep model=%s", scorer.title_model, scorer.deep_model)
    return profile, scorer


def startup_message(settings: Settings, scorer: JobScorer | None) -> str:
    boards = sorted({search.source for search in settings.searches})
    if scorer is not None:
        mode = (
            f"Model: {escape(scorer.deep_model)}. Alerting on ≥{settings.min_coverage_pct}% CV match, "
            f"junior-appropriate, location fits.\n"
            f"Near misses ({settings.digest_min_coverage_pct}–{settings.min_coverage_pct - 1}%) "
            f"digested daily at {settings.digest_hour:02d}:00."
        )
    else:
        mode = f"⚠️ Scoring is OFF ({API_KEY_ENV} or profile.yaml missing) — alerts are not CV-matched."
    return (
        f"🚀 job-radar started.\n"
        f"{len(settings.searches)} searches on {escape(', '.join(boards))}.\n"
        f"{mode}\n"
        f"Send /help for commands."
    )


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_path, secrets=(settings.telegram_bot_token, os.environ.get(API_KEY_ENV, "")))
    signal.signal(signal.SIGTERM, _raise_system_exit)
    started_at = time.time()

    store = Store(settings.db_path)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    # Only the boards config.yaml actually searches get a client.
    sources = {name: SOURCES[name]() for name in sorted({s.source for s in settings.searches})}
    jd_fetcher = JobDescriptionFetcher()
    profile, scorer = load_scoring(settings)
    evaluator = Evaluator(
        profile, scorer, jd_fetcher,
        deep_score_threshold=settings.deep_score_threshold,
        min_coverage_pct=settings.min_coverage_pct,
        years_stretch=settings.years_stretch,
    )
    pipeline = Pipeline(settings, store, sources, notifier, evaluator)

    log.info("Starting job-radar with %d searches.", len(settings.searches))
    notifier.send_text(startup_message(settings, scorer))

    # BackgroundScheduler, not Blocking: the cycle runs on a worker thread
    # so the main thread is free for the command listener below.
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        pipeline.run_cycle,
        trigger=IntervalTrigger(minutes=settings.poll_interval_minutes, jitter=settings.poll_jitter_seconds),
        next_run_time=datetime.datetime.now(),  # run once immediately on startup
        id="poll_cycle",
        max_instances=1,  # a slow cycle is skipped over, never run twice at once
        coalesce=True,
    )
    scheduler.add_job(
        pipeline.send_digest,
        trigger=CronTrigger(hour=settings.digest_hour, minute=0),
        id="daily_digest",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    listener = CommandListener(notifier, store, pipeline, settings, scheduler, started_at)
    try:
        listener.run()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down.")
    finally:
        listener.stop()
        scheduler.shutdown(wait=False)
        for closable in (*sources.values(), jd_fetcher, notifier, scorer, store):
            if closable is not None:
                closable.close()


if __name__ == "__main__":
    main()
