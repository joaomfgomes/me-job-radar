"""Telegram command listener: long-polls getUpdates and dispatches to commands.py.

Runs on the main thread while the scheduler (BackgroundScheduler) runs the
poll cycle on its own worker thread. getUpdates with timeout=25 blocks
server-side until an update arrives or the timeout expires, so this loop
does no local sleeping or busy-waiting while idle.
"""

from __future__ import annotations

import logging
import time

import httpx

from jobradar import commands
from jobradar.notify import TelegramNotifier
from jobradar.pipeline import Pipeline
from jobradar.store import Store

log = logging.getLogger(__name__)

POLL_TIMEOUT_SECONDS = 25
ERROR_BACKOFF_SECONDS = 5


class CommandListener:
    def __init__(self, notifier: TelegramNotifier, store: Store, pipeline: Pipeline, settings, scheduler, started_at: float) -> None:
        self._notifier = notifier
        self._store = store
        self._pipeline = pipeline
        self._settings = settings
        self._scheduler = scheduler
        self._started_at = started_at
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        offset = self._discard_backlog()
        log.info("Command listener started (long-polling getUpdates).")

        while self._running:
            try:
                updates = self._notifier.get_updates(offset=offset, timeout=POLL_TIMEOUT_SECONDS)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    log.error(
                        "getUpdates 409 Conflict -- another job-radar process is already "
                        "polling this bot (only one can call getUpdates at a time). "
                        "Retrying in %ss.", ERROR_BACKOFF_SECONDS,
                    )
                else:
                    log.warning("getUpdates HTTP error: %s. Retrying in %ss.", exc, ERROR_BACKOFF_SECONDS)
                time.sleep(ERROR_BACKOFF_SECONDS)
                continue
            except httpx.HTTPError as exc:
                log.warning("getUpdates network error: %s. Retrying in %ss.", exc, ERROR_BACKOFF_SECONDS)
                time.sleep(ERROR_BACKOFF_SECONDS)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self._handle_update(update)
                except Exception:
                    log.exception("Error handling update %s", update.get("update_id"))

    def _discard_backlog(self) -> int | None:
        """On startup, fetch only the latest pending update (if any) and
        discard it, so commands sent while the bot was down don't replay."""
        try:
            updates = self._notifier.get_updates(offset=-1, timeout=0)
        except httpx.HTTPError as exc:
            log.warning("Could not check for backlog updates: %s", exc)
            return None
        if not updates:
            return None
        return updates[-1]["update_id"] + 1

    def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message or "text" not in message:
            return

        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != str(self._notifier.chat_id):
            log.warning("Ignoring command from unauthorized chat_id=%s", chat_id)
            return

        log.info("Received command: %r", message["text"])

        next_run_at = None
        job = self._scheduler.get_job("poll_cycle")
        if job is not None and job.next_run_time is not None:
            next_run_at = job.next_run_time.timestamp()

        reply = commands.dispatch(
            message["text"],
            store=self._store,
            pipeline=self._pipeline,
            settings=self._settings,
            started_at=self._started_at,
            next_run_at=next_run_at,
        )
        self._notifier.send_text(reply)
