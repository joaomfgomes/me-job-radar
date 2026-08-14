"""Entry point: wires up the pipeline and runs it on a schedule.

Run directly with:  uv run python -m jobradar.main
"""

from __future__ import annotations

import datetime
import logging
import sys
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from jobradar.config import load_settings
from jobradar.listener import CommandListener
from jobradar.notify import TelegramNotifier
from jobradar.pipeline import Pipeline
from jobradar.sources.linkedin import LinkedInGuestSource
from jobradar.store import Store


def setup_logging(log_path: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path),
        ],
    )


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_path)
    log = logging.getLogger("jobradar")

    started_at = time.time()
    store = Store(settings.db_path)
    source = LinkedInGuestSource()
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    pipeline = Pipeline(settings, store, source, notifier)

    search_names = ", ".join(s.name for s in settings.searches)
    log.info("Starting job-radar. Watching: %s", search_names)
    notifier.send_text(
        f"🚀 job-radar started. Watching {len(settings.searches)} search(es): {search_names}\n"
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
    scheduler.start()

    listener = CommandListener(notifier, store, pipeline, settings, scheduler, started_at)

    try:
        listener.run()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down.")
    finally:
        listener.stop()
        scheduler.shutdown(wait=False)
        source.close()
        notifier.close()
        store.close()


if __name__ == "__main__":
    main()
