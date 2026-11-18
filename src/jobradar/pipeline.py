"""One poll cycle: fetch -> drop already-seen -> evaluate -> notify -> record.

This module is orchestration only. The question "should this job be
alerted?" lives in evaluation.py; this file runs the searches, keeps
per-search health counters, and makes sure every posting ends up recorded
exactly once.

Ordering guarantees worth knowing:

  * A job is evaluated before it is notified, and only jobs that pass are
    sent.
  * A job is marked seen only after its alert was delivered. If Telegram is
    down the job stays unseen and is retried next cycle -- a duplicate alert
    is a better failure than a lost one.
  * Jobs that are held back are recorded as fully as alerted ones, so
    /rejected, /stats and the daily digest can show what was filtered and
    why. Nothing disappears silently.
  * A repost -- same company and title as a job evaluated in the last
    REPOST_WINDOW_DAYS -- inherits that verdict instead of being evaluated
    again, and is never alerted twice (see Store.find_repost).
"""

from __future__ import annotations

import logging
import threading
import time
from html import escape

from jobradar.config import Settings
from jobradar.evaluation import Decision, Evaluator, Outcome
from jobradar.models import JobPosting, SearchConfig
from jobradar.notify import TelegramNotifier
from jobradar.sources.base import JobSource, RateLimitedError
from jobradar.store import Store

log = logging.getLogger(__name__)

REPOST_WINDOW_DAYS = 30


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        sources: dict[str, JobSource],
        notifier: TelegramNotifier,
        evaluator: Evaluator,
    ) -> None:
        self.settings = settings
        self.store = store
        self.sources = sources  # keyed by SearchConfig.source
        self.notifier = notifier
        self.evaluator = evaluator
        # Health, kept in memory (it resets on restart, which is fine) and
        # read by /status from the listener thread -- hence the lock.
        self._health_lock = threading.Lock()
        self._last_poll_at: float | None = None
        self._last_success_at: float | None = None
        self._consecutive_errors: dict[str, int] = {}
        self._consecutive_empty: dict[str, int] = {}

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------

    def run_cycle(self) -> None:
        with self._health_lock:
            self._last_poll_at = time.time()
        for search in self.settings.searches:
            try:
                self._run_search(search)
            except Exception:
                # Isolation: one search failing past its own handlers (a DB
                # error, a parser edge case) must not cost the others their
                # turn this cycle.
                log.exception("Unexpected error while running search '%s'", search.name)

    def _run_search(self, search: SearchConfig) -> None:
        try:
            postings = self.sources[search.source].fetch(search)
        except Exception as exc:
            if isinstance(exc, RateLimitedError):
                log.warning("Rate limited on '%s': %s", search.name, exc)
            else:
                log.exception("Fetch failed for search '%s'", search.name)
            self._count_failure(search, self._consecutive_errors, self.settings.consecutive_error_alert_threshold,
                                "has failed {count} fetches in a row (rate limited or request error).")
            return

        with self._health_lock:
            self._last_success_at = time.time()
            self._consecutive_errors[search.name] = 0

        if not postings:
            log.info("'%s': 0 jobs fetched", search.name)
            self._count_failure(search, self._consecutive_empty, self.settings.consecutive_empty_alert_threshold,
                                "has returned zero jobs for {count} cycles in a row -- the board may have "
                                "changed its markup.")
            return
        with self._health_lock:
            self._consecutive_empty[search.name] = 0

        # First time this search has ever run: record silently, don't alert.
        if not self.store.is_seeded(search.name):
            for posting in postings:
                self.store.mark_seen(posting)
            self.store.mark_seeded(search.name)
            log.info("Seeded '%s' with %d existing jobs (no alerts sent)", search.name, len(postings))
            return

        new_postings = self.store.filter_unseen(postings)
        log.info("'%s': %d jobs fetched, %d new", search.name, len(postings), len(new_postings))
        for posting in new_postings:
            self._process(posting)

    def _process(self, posting: JobPosting) -> None:
        original = self.store.find_repost(posting, days=REPOST_WINDOW_DAYS)
        if original is not None:
            verdict = "alerted" if original["notified"] else original["outcome"]
            decision = Decision(Outcome.REPOST, f"repost of a job already evaluated ({verdict}): {original['reason']}")
        else:
            decision = self.evaluator.evaluate(posting)

        if decision.notify:
            warnings = decision.warnings
            if decision.outcome is Outcome.UNFILTERED:
                warnings = (f"Not filtered — {decision.reason}", *warnings)
            delivered = self.notifier.send_job(posting, score=decision.score, deep=decision.deep, warnings=warnings)
            if delivered is None:
                # Leave it unseen so the next cycle retries.
                log.error("Notify failed for job %s ('%s') -- will retry next cycle", posting.job_id, posting.title)
                return

        self.store.mark_seen(posting)
        self._record(posting, decision)
        log.info("'%s': %s (%s)", posting.title, "ALERTED" if decision.notify else "held back", decision.reason)

    def _record(self, posting: JobPosting, decision: Decision) -> None:
        """Persist whatever the cascade determined, however far it got."""
        deep = decision.deep if decision.deep is not None and not decision.deep.unscored else None
        self.store.record_verdict(
            posting.dedup_key,
            outcome=decision.outcome.value,
            reason=decision.reason,
            notified=decision.notify,
            score=decision.score,
            title_score=decision.title_score,
            title_score_reason=decision.title_score_reason,
            coverage_pct=deep.coverage_pct if deep else None,
            requirements=[
                {"requirement": r.requirement, "met": r.met, "evidence": r.evidence} for r in deep.requirements
            ] if deep else None,
            interview_chance_pct=deep.interview_chance_pct if deep else None,
            chance_reason=deep.chance_reason if deep else None,
        )
        if deep is not None and deep.insights is not None:
            self.store.save_insights(posting.dedup_key, deep.insights)

    # ------------------------------------------------------------------
    # Daily digest
    # ------------------------------------------------------------------

    def send_digest(self) -> None:
        """Daily summary of near misses -- jobs worth knowing about but
        below the alert bar. Makes an over-tight min_coverage_pct visible."""
        rows = self.store.digest_candidates(
            min_pct=self.settings.digest_min_coverage_pct,
            max_pct=self.settings.min_coverage_pct,
        )
        if not rows:
            log.info("Digest: no near-miss jobs today.")
        elif self.notifier.send_digest(rows, self.settings.min_coverage_pct):
            self.store.mark_digest_sent([row["job_id"] for row in rows])
            log.info("Digest: sent %d near-miss job(s).", len(rows))
        else:
            log.warning("Digest: send failed -- will retry in tomorrow's digest.")

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_snapshot(self) -> dict:
        with self._health_lock:
            return {
                "last_poll_at": self._last_poll_at,
                "last_success_at": self._last_success_at,
                "consecutive_errors": dict(self._consecutive_errors),
                "consecutive_empty": dict(self._consecutive_empty),
            }

    def _count_failure(self, search: SearchConfig, counters: dict[str, int], threshold: int, message: str) -> None:
        """Bump a per-search failure counter and warn once, on the cycle it
        reaches the threshold. The counter is reset by the next success, so
        a problem that comes back is reported again."""
        with self._health_lock:
            count = counters[search.name] = counters.get(search.name, 0) + 1
        if count == threshold:
            self.notifier.send_text(
                f"⚠️ job-radar: '{escape(search.name, quote=False)}' {message.format(count=count)} Check the log."
            )
