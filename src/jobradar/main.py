"""Entry point: wires up the pipeline and runs it on a schedule.

Run directly with:  uv run python -m jobradar.main
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
from jobradar.listener import CommandListener
from jobradar.llm import API_KEY_ENV
from jobradar.logging_setup import setup_logging
from jobradar.notify import TelegramNotifier
from jobradar.pipeline import Pipeline
from jobradar.profile import Profile, load_profile
from jobradar.scoring import JobScorer
from jobradar.sources.linkedin import LinkedInGuestSource
from jobradar.sources.linkedin_jd import JobDescriptionFetcher
from jobradar.sources.wellfound import WellfoundSource
from jobradar.store import Store


def _raise_system_exit(signum, _frame) -> None:
    """launchd/systemd stop a service with SIGTERM, which by default kills
    Python without unwinding -- turning it into SystemExit lets main()'s
    finally block close the HTTP clients and the SQLite connection."""
    raise SystemExit(0)


def load_scoring(settings: Settings, log: logging.Logger) -> tuple[Profile | None, JobScorer | None]:
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

    log.info("Scoring enabled: profile=%s, title model=%s, deep model=%s",
             profile_path, scorer.title_model, scorer.deep_model)
    return profile, scorer


def main() -> None:
    settings = load_settings()
    setup_logging(
        settings.log_path,
        secrets=(settings.telegram_bot_token, os.environ.get(API_KEY_ENV, "")),
    )
    signal.signal(signal.SIGTERM, _raise_system_exit)
    log = logging.getLogger("jobradar")

    started_at = time.time()
    store = Store(settings.db_path)
    sources = {"linkedin": LinkedInGuestSource(), "wellfound": WellfoundSource()}
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    profile, scorer = load_scoring(settings, log)
    # No auth needed (same guest-endpoint posture as LinkedInGuestSource) --
    # only worth having when there's a scorer to feed its output to.
    jd_fetcher = JobDescriptionFetcher() if scorer is not None else None
    if jd_fetcher is not None:
        log.info(
            "Enforcement on: alerting only jobs with >= %d%% requirement coverage that are "
            "junior-appropriate. Near-misses from %d%% go into the daily digest at %02d:00.",
            settings.min_coverage_pct, settings.digest_min_coverage_pct, settings.digest_hour,
        )
    pipeline = Pipeline(settings, store, sources, notifier, profile=profile, scorer=scorer, jd_fetcher=jd_fetcher)

    core, secondary = pipeline.active_searches()
    locations = sorted({s.location_name for s in core + secondary if s.location_name})
    board_names = sorted({s.source for s in core + secondary})
    log.info("Starting job-radar. %d core + %d rotating searches across %s.",
             len(core), len(secondary), ", ".join(board_names))
    if scorer is not None:
        mode = (
            f"Model: {escape(scorer.deep_model)}. Alerting on ≥{settings.min_coverage_pct}% CV match, "
            f"junior-appropriate, location fits.\n"
            f"Near misses ({settings.digest_min_coverage_pct}-{settings.min_coverage_pct - 1}%) "
            f"digested daily at {settings.digest_hour:02d}:00.\n"
        )
    else:
        mode = f"⚠️ Scoring is OFF ({API_KEY_ENV} or profile.yaml missing) -- alerts are not CV-matched.\n"
    notifier.send_text(
        f"🚀 job-radar started.\n"
        f"Boards: {escape(', '.join(board_names), quote=False)}\n"
        f"Locations: {escape(', '.join(locations), quote=False)}\n"
        f"{mode}"
        f"Send /help for commands."
    )

    # BackgroundScheduler (not Blocking): the poll cycle runs on its own
    # worker thread so the main thread is free to run the command listener's
    # long-poll loop below.
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        pipeline.run_cycle,
        trigger=IntervalTrigger(minutes=settings.poll_interval_minutes, jitter=settings.poll_jitter_seconds),
        next_run_time=datetime.datetime.now(),  # run once immediately on startup
        id="poll_cycle",
        max_instances=1,
        coalesce=True,
    )
    if scorer is not None:
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
        for source in sources.values():
            source.close()
        notifier.close()
        if scorer is not None:
            scorer.close()
        if jd_fetcher is not None:
            jd_fetcher.close()
        store.close()


if __name__ == "__main__":
    main()
