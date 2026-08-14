"""The fetch -> dedup -> notify -> mark_seen cycle, with backoff and a heartbeat.

Ordering matters: we notify BEFORE marking a job as seen. If the Telegram
call fails, the job stays "unseen" and gets retried next cycle. A crash
mid-cycle gives you a duplicate alert instead of a silently dropped job --
for an alerting system, that's the right failure mode.
"""

from __future__ import annotations

import logging

from jobradar.config import Settings
from jobradar.models import SearchConfig
from jobradar.notify import TelegramNotifier
from jobradar.sources.linkedin import LinkedInGuestSource, RateLimitedError
from jobradar.store import Store

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings, store: Store, source: LinkedInGuestSource, notifier: TelegramNotifier) -> None:
        self.settings = settings
        self.store = store
        self.source = source
        self.notifier = notifier
        # per-search health tracking, kept in memory (resets on restart -- fine)
        self._consecutive_errors: dict[str, int] = {}
        self._consecutive_empty: dict[str, int] = {}
        self._error_alerted: dict[str, bool] = {}
        self._empty_alerted: dict[str, bool] = {}

    def run_cycle(self) -> None:
        for search in self.settings.searches:
            self._run_search(search)

    def _run_search(self, search: SearchConfig) -> None:
        try:
            postings = self.source.fetch(search)
        except RateLimitedError as exc:
            log.warning("Rate limited on '%s': %s", search.name, exc)
            self._handle_error(search)
            return
        except Exception:
            log.exception("Fetch failed for search '%s'", search.name)
            self._handle_error(search)
            return

        # fetch succeeded -- reset error tracking
        self._consecutive_errors[search.name] = 0
        self._error_alerted[search.name] = False

        if not postings:
            self._handle_empty(search)
            return
        self._consecutive_empty[search.name] = 0
        self._empty_alerted[search.name] = False

        # First time this search has ever run: seed silently, don't notify.
        # Prevents a fresh/lost state.db from blasting every job currently
        # in the time window as if it were new.
        if not self.store.is_seeded(search.name):
            for p in postings:
                self.store.mark_seen(p)
            self.store.mark_seeded(search.name)
            log.info("Seeded '%s' with %d existing jobs (no alerts sent)", search.name, len(postings))
            return

        new_postings = self.store.filter_unseen(postings)
        if not new_postings:
            log.info("'%s': %d jobs fetched, none new", search.name, len(postings))
            return

        log.info("'%s': %d new job(s)", search.name, len(new_postings))
        for p in new_postings:
            if self.notifier.send_job(p):
                self.store.mark_seen(p)
            else:
                log.error("Notify failed for job %s ('%s') -- will retry next cycle", p.job_id, p.title)

    def _handle_error(self, search: SearchConfig) -> None:
        count = self._consecutive_errors.get(search.name, 0) + 1
        self._consecutive_errors[search.name] = count
        threshold = self.settings.consecutive_error_alert_threshold
        if count >= threshold and not self._error_alerted.get(search.name):
            self.notifier.send_text(
                f"⚠️ job-radar: '{search.name}' has failed {count} fetches in a row "
                f"(rate limited or request error). Check the logs."
            )
            self._error_alerted[search.name] = True

    def _handle_empty(self, search: SearchConfig) -> None:
        count = self._consecutive_empty.get(search.name, 0) + 1
        self._consecutive_empty[search.name] = count
        threshold = self.settings.consecutive_empty_alert_threshold
        log.info("'%s': 0 jobs fetched (%d consecutive)", search.name, count)
        if count >= threshold and not self._empty_alerted.get(search.name):
            self.notifier.send_text(
                f"⚠️ job-radar: '{search.name}' has returned zero jobs for {count} "
                f"consecutive cycles. LinkedIn may have changed its markup -- "
                f"the parser might be broken. Check the logs."
            )
            self._empty_alerted[search.name] = True
