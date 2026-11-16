"""One poll cycle: fetch -> drop already-seen -> evaluate -> notify -> record.

This module is the orchestration only. The question "should this job be
alerted?" lives in evaluation.py; this file decides which searches run,
keeps per-search health counters, and makes sure every posting ends up
recorded exactly once.

Ordering guarantees worth knowing:

  * A job is evaluated before it is notified, and only jobs that pass are
    sent.
  * A job is marked seen only after its alert was delivered. If Telegram is
    down the job stays unseen and is retried next cycle -- a duplicate alert
    is a better failure than a lost one.
  * Jobs that are held back are recorded as fully as alerted ones, so
    /rejected, /stats and the daily digest can show what was filtered and
    why. Nothing disappears silently.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from html import escape

from jobradar.config import Settings, expand_matrix_with_locations
from jobradar.evaluation import Decision, Evaluator, Outcome, Thresholds
from jobradar.location_validation import validate_location
from jobradar.models import JobPosting, SearchConfig
from jobradar.notify import TelegramNotifier
from jobradar.profile import Profile
from jobradar.scoring import JobScorer
from jobradar.sources.base import JobSource, RateLimitedError
from jobradar.sources.linkedin_jd import JobDescriptionFetcher
from jobradar.store import Store

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        sources: dict[str, JobSource],
        notifier: TelegramNotifier,
        profile: Profile | None = None,
        scorer: JobScorer | None = None,
        jd_fetcher: JobDescriptionFetcher | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        # keyed by SearchConfig.source, so each search runs against its own board
        self.sources = sources
        self.notifier = notifier
        # Without a profile nothing is filtered; without a scorer only the
        # deterministic prefilter runs. See evaluation.py.
        self.evaluator = Evaluator(
            profile, scorer, jd_fetcher,
            Thresholds(settings.deep_score_threshold, settings.min_coverage_pct),
        )
        # per-search health tracking, kept in memory (resets on restart -- fine)
        self._consecutive_errors: dict[str, int] = {}
        self._consecutive_empty: dict[str, int] = {}
        self._error_alerted: dict[str, bool] = {}
        self._empty_alerted: dict[str, bool] = {}
        # overall health, read by the /status command from a different thread
        self._health_lock = threading.Lock()
        self.last_poll_at: float | None = None
        self.last_success_at: float | None = None
        # round-robin cursor into secondary_searches,
        # resets on restart -- harmless, just starts the rotation over
        self._secondary_rotation_index = 0

    def run_cycle(self) -> None:
        with self._health_lock:
            self.last_poll_at = time.time()
        core, secondary = self.active_searches()
        for search in core + self._next_secondary_batch(secondary):
            try:
                self._run_search(search)
            except Exception:
                # Isolation: one search failing past its own handlers (a DB
                # error, a parser edge case) must not cost every other
                # search its turn this cycle.
                log.exception("Unexpected error while running search '%s'", search.name)

    def active_searches(self) -> tuple[list[SearchConfig], list[SearchConfig]]:
        """current_searches() minus paused locations. Dropping them before
        the rotation picks its batch means a paused location never uses up
        one of the cycle's slots."""
        core, secondary = self.current_searches()
        disabled = self._disabled_locations()
        return (
            [s for s in core if s.location_name not in disabled],
            [s for s in secondary if s.location_name not in disabled],
        )

    def current_searches(self) -> tuple[list[SearchConfig], list[SearchConfig]]:
        """Re-expands the search matrix every cycle against config.yaml's
        locations PLUS anything added at runtime via /countries add, so a
        newly-added location is searched on the very next cycle -- no
        restart. Cheap: pure dict iteration, no I/O."""
        configured = list(self.settings.matrix_locations)
        configured_names = {loc["name"].lower() for loc in configured}
        # config.yaml wins on a name clash: a location added from Telegram
        # and later written into config.yaml must not be searched twice.
        locations = configured + [
            {"name": name, **queries}
            for name, queries in self.store.custom_locations().items()
            if name.lower() not in configured_names
        ]
        matrix_core, matrix_secondary = expand_matrix_with_locations(locations, self.settings.matrix_sources)
        return (
            matrix_core + list(self.settings.searches),
            matrix_secondary + list(self.settings.secondary_searches),
        )

    def _disabled_locations(self) -> set[str]:
        """Read fresh every cycle so a /countries toggle takes effect on the
        next poll without a restart. Locations absent from the table are
        enabled, so adding a country to config.yaml needs no DB write."""
        return {name for name, enabled in self.store.location_prefs().items() if not enabled}

    def _next_secondary_batch(self, pool: list[SearchConfig]) -> list[SearchConfig]:
        """Round-robin through the secondary pool, secondary_batch_size per
        cycle, instead of polling all of them every cycle. Coverage grows
        without multiplying request volume 1:1 -- see the Settings docstring
        for the traffic math. The pool can change size between calls (a
        location added or removed via /countries) -- the modulo below
        already handles that safely, it just needs the pool passed in fresh."""
        if not pool:
            return []
        batch_size = min(self.settings.secondary_batch_size, len(pool))
        batch = []
        for _ in range(batch_size):
            batch.append(pool[self._secondary_rotation_index % len(pool)])
            self._secondary_rotation_index += 1
        return batch

    def health_snapshot(self) -> dict:
        with self._health_lock:
            return {
                "last_poll_at": self.last_poll_at,
                "last_success_at": self.last_success_at,
                "consecutive_errors": dict(self._consecutive_errors),
                "consecutive_empty": dict(self._consecutive_empty),
            }

    def _run_search(self, search: SearchConfig) -> None:
        source = self.sources.get(search.source)
        if source is None:
            log.warning("No source configured for '%s' (source=%s) -- skipping", search.name, search.source)
            return
        try:
            postings = source.fetch(search)
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
        with self._health_lock:
            self.last_success_at = time.time()

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
            self._process(p)

    def _process(self, posting: JobPosting) -> None:
        decision = self.evaluator.evaluate(posting)

        if not decision.notify:
            log.info("'%s': held back (%s)", posting.title, decision.reason)
            self.store.mark_seen(posting)
            self._record(posting, decision)
            return

        delivered = self.notifier.send_job(
            posting,
            score=decision.score,
            deep=decision.deep,
            language_unverified=decision.language_unverified,
            unfiltered_reason=decision.reason if decision.outcome is Outcome.UNFILTERED else None,
        )
        if delivered is None:
            # Leave it unseen so the next cycle retries.
            log.error("Notify failed for job %s ('%s') -- will retry next cycle", posting.job_id, posting.title)
            return

        self.store.mark_seen(posting)
        self.store.set_notified(posting.dedup_key)
        self._record(posting, decision)
        log.info("'%s': ALERTED (%s)", posting.title, decision.reason)

    def _record(self, posting: JobPosting, decision: Decision) -> None:
        """Persist whatever the cascade determined, however far it got."""
        key = posting.dedup_key
        self.store.set_outcome(key, decision.outcome.value)
        if decision.title_score is not None:
            self.store.set_title_score(key, decision.title_score, decision.title_score_reason or "")

        deep = decision.deep
        if deep is None or deep.unscored:
            self.store.set_score(key, decision.title_score, decision.reason)
            return

        self.store.set_deep_result(
            key,
            score=deep.score,
            reason=deep.reason,
            coverage_pct=deep.coverage_pct,
            requirements_json=json.dumps([
                {"requirement": r.requirement, "met": r.met, "evidence": r.evidence}
                for r in deep.requirements
            ]),
            is_junior_appropriate=deep.is_junior_appropriate,
            interview_chance_pct=deep.interview_chance_pct,
            chance_reason=deep.chance_reason,
        )
        self.store.set_score(key, deep.score, decision.reason)
        if deep.insights is not None:
            self.store.save_insights(key, deep.insights)

    def validate_and_add_location(self, name: str) -> str:
        """Runs live validation (location_validation.validate_location)
        against every configured board, using short-lived source instances
        rather than self.sources: this runs on the Telegram listener thread
        while polling runs on the scheduler thread, and httpx.Client isn't
        guaranteed safe for concurrent cross-thread use, so a fresh
        throwaway instance sidesteps the question entirely.

        Persists only the boards that actually validated -- a location can
        be real on one board's vocabulary and not another's (see
        location_validation.py's module docstring). Returns a
        human-readable report to send back to the user."""
        if not self.settings.matrix_sources:
            return "No search_matrix configured in config.yaml -- nothing to validate against."

        queries: dict[str, str] = {}
        # The reply goes out with parse_mode=HTML. `name` is user-typed and
        # the validation reasons quote scraped location strings, so each is
        # escaped -- an unescaped '<' would make Telegram reject the reply.
        safe_name = escape(name, quote=False)
        lines = [f"Validating '{safe_name}'..."]
        for source_name, spec in self.settings.matrix_sources.items():
            existing = self.sources.get(source_name)
            if existing is None:
                lines.append(f"  {source_name}: not configured, skipped")
                continue

            spec = spec or {}
            keywords = (spec.get("core_keywords") or spec.get("secondary_keywords") or ["AI Engineer"])[0]
            # Deliberately wider than whatever config.yaml uses for live
            # polling: validation just needs a confident sample that the
            # location resolves, not a realistic live search -- a low-volume
            # city/board combination (e.g. a single small city on a
            # low-traffic board) can otherwise fall below MIN_SAMPLE_SIZE
            # within the live window and read as a false rejection.
            window = 30 * 86400

            fresh = type(existing)()
            try:
                result = validate_location(name, fresh, keywords=keywords, time_posted_seconds=window)
            except Exception as exc:
                lines.append(f"  ❌ {source_name}: validation failed ({escape(str(exc), quote=False)})")
                continue
            finally:
                fresh.close()

            if result.passed:
                queries[source_name] = result.query_used
                lines.append(
                    f"  ✅ {source_name}: {escape(result.reason, quote=False)} "
                    f"(query used: {escape(repr(result.query_used), quote=False)})"
                )
            else:
                lines.append(f"  ❌ {source_name}: {escape(result.reason, quote=False)}")

        if not queries:
            lines.append("\nNot added -- no board recognized that location. Try a different spelling?")
            return "\n".join(lines)

        self.store.add_custom_location(name, queries)
        lines.append(f"\nAdded '{safe_name}' (validated on: {', '.join(queries)}).")
        warning = self._rotation_warning()
        if warning:
            lines.append(warning)
        return "\n".join(lines)

    def _rotation_warning(self) -> str:
        """Adding a location grows the secondary pool, which lengthens a
        full rotation (pool_size / secondary_batch_size cycles) -- if that
        now exceeds a search's own window, postings between visits go
        unseen. This is exactly the bug found and fixed earlier for the
        original 18-search/2-per-cycle config; warn instead of silently
        reintroducing it."""
        _, secondary = self.current_searches()
        if not secondary or self.settings.secondary_batch_size <= 0:
            return ""
        cycles = -(-len(secondary) // self.settings.secondary_batch_size)  # ceil
        rotation_minutes = cycles * self.settings.poll_interval_minutes
        narrowest_minutes = min(s.time_posted_seconds for s in secondary) / 60
        if rotation_minutes > narrowest_minutes:
            return (
                f"⚠️ Rotation now takes ~{rotation_minutes}min for {len(secondary)} secondary "
                f"searches, but the narrowest window is {narrowest_minutes:.0f}min -- some postings "
                f"may be missed between visits. Consider raising secondary_batch_size in config.yaml."
            )
        return f"Rotation: {len(secondary)} secondary searches, ~{rotation_minutes}min per cycle (within window)."

    def send_digest(self) -> None:
        """Daily summary of near-misses -- jobs good enough to be worth
        knowing about but below the alert bar. Makes an over-tight
        min_coverage_pct visible instead of silent."""
        rows = self.store.digest_candidates(
            min_pct=self.settings.digest_min_coverage_pct,
            max_pct=self.settings.min_coverage_pct,
        )
        if not rows:
            log.info("Digest: no near-miss jobs today.")
            return

        if self.notifier.send_digest(rows, self.settings.min_coverage_pct):
            self.store.mark_digest_sent([r["job_id"] for r in rows])
            log.info("Digest: sent %d near-miss job(s).", len(rows))
        else:
            log.warning("Digest: send failed -- will retry in tomorrow's digest.")

    def _handle_error(self, search: SearchConfig) -> None:
        count = self._consecutive_errors.get(search.name, 0) + 1
        self._consecutive_errors[search.name] = count
        threshold = self.settings.consecutive_error_alert_threshold
        if count >= threshold and not self._error_alerted.get(search.name):
            self.notifier.send_text(
                f"⚠️ job-radar: '{escape(search.name, quote=False)}' has failed {count} fetches in a row "
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
                f"⚠️ job-radar: '{escape(search.name, quote=False)}' has returned zero jobs for {count} "
                f"consecutive cycles. {escape(search.source, quote=False)} may have changed its markup -- "
                f"the parser might be broken. Check the logs."
            )
            self._empty_alerted[search.name] = True
