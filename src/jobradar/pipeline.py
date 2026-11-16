"""The fetch -> dedup -> evaluate -> notify -> mark_seen cycle, with backoff
and a heartbeat.

Ordering: a job is fully evaluated BEFORE it is notified, and only jobs that
clear the bar are ever sent (see _evaluate). This inverts the original
notify-first design, which existed to keep alerts near-instant while scoring
was observation-only. Now that scoring actually decides what gets through,
notifying first would defeat the point -- and with only a handful of jobs a
day surviving the filter, the few seconds of added latency cost nothing.
Notification failures still leave the job unseen so the next cycle retries.

The evaluation is a cascade, cheapest stage first, and every stage FAILS
OPEN -- an infrastructure problem must never silently swallow a real job:

  1. prefilter (free, deterministic)  -- title/employer dealbreakers and
     seniority terms above the candidate's ceiling. Certainties only.
  2. JD fetch                         -- fetch failure => notify anyway.
  3. language check (free)            -- description not in a language the
     candidate reads. Needs the JD body: tech job titles stay in English
     even when the posting itself doesn't.
  4. title score (cheap model)        -- below deep_score_threshold, the job
     is noise; this is the ~87% the deep pass never has to pay for.
  5. deep score (stronger model)      -- enumerates the posting's must-have
     requirements and judges each against the CV. Coverage is computed from
     that list in Python, never asked of the model. Two seniority gates run
     BEFORE coverage is even considered, and both are strict on purpose (no
     benefit of the doubt): a JD-stated years_required above
     max_years_experience is a hard reject regardless of how well the CV
     otherwise matches (an extracted fact, checked in Python -- not trusted
     to the model's own judgment); is_junior_appropriate is the model's
     holistic fallback for postings that read as senior without stating a
     number. A job is alerted only if it clears both AND
     coverage >= min_coverage_pct.

Anything scored but held back is still recorded, so nothing disappears
silently: near-misses (>= digest_min_coverage_pct) are summarized once a
day by send_digest(), and /rejected shows the rest on demand.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from html import escape

from jobradar.config import Settings, expand_matrix_with_locations
from jobradar.cv_ingest import format_cv_context
from jobradar.lang_filter import detect_language_mismatch
from jobradar.location_validation import validate_location
from jobradar.models import JobPosting, SearchConfig
from jobradar.notify import TelegramNotifier
from jobradar.prefilter import prefilter
from jobradar.profile import Profile
from jobradar.scoring import DeepScoreResult, OpenAIScorer
from jobradar.sources.linkedin import RateLimitedError
from jobradar.sources.linkedin_jd import JobDescriptionFetcher
from jobradar.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    """Outcome of evaluating one posting. `reason` explains the verdict
    either way and is what gets stored/shown for suppressed jobs."""

    notify: bool
    reason: str
    deep: DeepScoreResult | None = None
    title_score: int | None = None
    title_score_reason: str | None = None
    # True only for notify=True paths that never reached the language check
    # (no JD text at all -- see _evaluate) -- distinct from title/deep
    # scoring failing later, where language WAS already verified. Lets the
    # alert say plainly "this one wasn't screened" instead of silently
    # possibly showing a job in a language the candidate can't read.
    language_unverified: bool = False

    @property
    def score(self) -> int | None:
        """The score to act on: the deep pass's if it produced one, else the
        title pass's. Fail-open paths land on the title score rather than
        None, so a job that slipped through on an API error still carries
        whatever judgment we did manage to get."""
        if self.deep is not None and not self.deep.unscored:
            return self.deep.score
        return self.title_score


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        sources: dict[str, object],
        notifier: TelegramNotifier,
        profile: Profile | None = None,
        scorer: OpenAIScorer | None = None,
        jd_fetcher: JobDescriptionFetcher | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        # keyed by SearchConfig.source, so each search runs against its own board
        self.sources = sources
        self.notifier = notifier
        # profile + scorer must both be set for any filtering to happen at
        # all; without them the pipeline notifies everything it finds, as it
        # did before scoring existed. jd_fetcher additionally enables the
        # language check and the deep pass -- see module docstring.
        self.profile = profile
        self.scorer = scorer
        self.jd_fetcher = jd_fetcher
        # per-search health tracking, kept in memory (resets on restart -- fine)
        self._consecutive_errors: dict[str, int] = {}
        self._consecutive_empty: dict[str, int] = {}
        self._error_alerted: dict[str, bool] = {}
        self._empty_alerted: dict[str, bool] = {}
        # overall health, read by the /status command from a different thread
        self._health_lock = threading.Lock()
        self.last_poll_at: float | None = None
        self.last_success_at: float | None = None
        # query expansion (step 3): round-robin cursor into secondary_searches,
        # resets on restart -- harmless, just starts the rotation over
        self._secondary_rotation_index = 0

    def run_cycle(self) -> None:
        with self._health_lock:
            self.last_poll_at = time.time()
        core, secondary = self.current_searches()
        disabled = self._disabled_locations()
        for search in core + self._next_secondary_batch(secondary):
            if search.location_name in disabled:
                continue
            try:
                self._run_search(search)
            except Exception:
                # Isolation: one search blowing up past its own handlers (a
                # DB error, a parser edge case mid-evaluation) must not cost
                # every other search its turn this cycle.
                log.exception("Unexpected error while running search '%s'", search.name)

    def current_searches(self) -> tuple[list[SearchConfig], list[SearchConfig]]:
        """Re-expands the search matrix every cycle against config.yaml's
        locations PLUS anything added at runtime via /countries add, so a
        newly-added location is searched on the very next cycle -- no
        restart. Cheap: pure dict iteration, no I/O."""
        custom = self.store.custom_locations()
        locations = list(self.settings.matrix_locations) + [
            {"name": name, **queries} for name, queries in custom.items()
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
        decision = self._evaluate(posting)

        if not decision.notify:
            log.info("'%s': held back (%s)", posting.title, decision.reason)
            self.store.mark_seen(posting)
            self._record(posting, decision)
            return

        if self.notifier.send_job(
            posting, score=decision.score, deep=decision.deep, language_unverified=decision.language_unverified
        ) is None:
            # Leave it unseen so the next cycle retries -- a duplicate alert
            # is a better failure than a dropped job.
            log.error("Notify failed for job %s ('%s') -- will retry next cycle", posting.job_id, posting.title)
            return

        self.store.mark_seen(posting)
        self.store.set_notified(posting.dedup_key)
        self._record(posting, decision)
        log.info("'%s': ALERTED (%s)", posting.title, decision.reason)

    def _record(self, posting: JobPosting, decision: Decision) -> None:
        """Persist whatever the cascade managed to determine, however far it
        got -- suppressed jobs are recorded just as fully as alerted ones so
        /rejected and the digest have something to show."""
        if decision.title_score is not None:
            self.store.set_title_score(posting.dedup_key, decision.title_score, decision.title_score_reason or "")

        deep = decision.deep
        if deep is not None and not deep.unscored:
            self.store.set_deep_result(
                posting.dedup_key,
                score=deep.score,
                reason=deep.reason,
                coverage_pct=deep.coverage_pct,
                requirements_json=json.dumps([
                    {"requirement": r.requirement, "met": r.met, "evidence": r.evidence}
                    for r in deep.requirements
                ]),
                is_junior_appropriate=deep.is_junior_appropriate,
            )
            self.store.set_score(posting.dedup_key, deep.score, decision.reason)
        else:
            self.store.set_score(posting.dedup_key, decision.title_score, decision.reason)

    def _evaluate(self, posting: JobPosting) -> Decision:
        """Runs the cascade described in the module docstring. Every failure
        path returns notify=True: we would rather send a job that turns out
        to be irrelevant than drop one because an API call timed out."""
        if self.profile is None or self.scorer is None:
            return Decision(notify=True, reason="scoring disabled")

        pre = prefilter(posting, self.profile)
        if not pre.passed:
            return Decision(notify=False, reason=f"prefilter: {pre.reason}")

        # Some boards state the experience floor outright (Wellfound does).
        # When they do it's a fact, not an inference, so it settles seniority
        # before any model is asked -- and costs nothing.
        ceiling = self.profile.max_years_experience
        stated = posting.years_experience_min
        if ceiling is not None and stated is not None and stated > ceiling:
            return Decision(notify=False, reason=f"posting requires {stated}+ years, above the {ceiling}-year ceiling")

        # A board that returns the description with the search result spares
        # us a whole extra request -- and the stages below need that text
        # regardless of where it came from.
        jd_text = posting.description
        if jd_text is None and self.jd_fetcher is None:
            # Title-only mode: no description to check language or
            # requirements against, so the title score is all we have.
            title = self.scorer.score(posting, self.profile)
            return Decision(
                notify=True,
                reason="no JD fetcher configured -- title score only",
                title_score=title.score,
                title_score_reason=title.reason,
                language_unverified=True,
            )

        if jd_text is None:
            jd_text = self.jd_fetcher.fetch(posting.job_id)
        if jd_text is None:
            return Decision(notify=True, reason="JD fetch failed -- alerted unfiltered", language_unverified=True)

        mismatch = detect_language_mismatch(jd_text, self.profile.languages)
        if mismatch:
            return Decision(notify=False, reason=f"language: {mismatch}")

        title = self.scorer.score(posting, self.profile)
        if title.unscored:
            return Decision(
                notify=True,
                reason=f"title scoring failed -- alerted unfiltered ({title.reason})",
                title_score=None,
                title_score_reason=title.reason,
            )
        if title.score < self.settings.deep_score_threshold:
            return Decision(
                notify=False,
                reason=f"title score {title.score}/10 below deep threshold: {title.reason}",
                title_score=title.score,
                title_score_reason=title.reason,
            )

        deep = self.scorer.score_deep(
            posting,
            self.profile,
            jd_text,
            title_score=title.score,
            title_score_reason=title.reason,
            cv_context=format_cv_context(),  # None if profile.generated.yaml is absent -- fine
        )
        return self._judge_deep(deep, title.score, title.reason)

    def _judge_deep(self, deep: DeepScoreResult, title_score: int, title_reason: str) -> Decision:
        common = {"deep": deep, "title_score": title_score, "title_score_reason": title_reason}

        if deep.unscored:
            return Decision(notify=True, reason=f"deep scoring failed -- alerted unfiltered ({deep.reason})", **common)

        # Enforced as an extracted fact, not the model's is_junior_appropriate
        # judgment call below -- "4+ years" in the JD means 4+ years, full
        # stop, no interpretation. Checked first because it's the least
        # ambiguous signal available.
        ceiling = self.profile.max_years_experience
        if deep.years_required is not None and ceiling is not None and deep.years_required > ceiling:
            return Decision(
                notify=False,
                reason=f"posting requires {deep.years_required}+ years (JD-stated), above the {ceiling}-year ceiling",
                **common,
            )

        if not deep.is_junior_appropriate:
            return Decision(notify=False, reason=f"not junior-appropriate: {deep.reason}", **common)

        coverage = deep.coverage_pct
        if coverage is None:
            # Model returned no requirements at all -- treating that as 0%
            # would suppress on a modelling artefact, so fail open instead.
            # (Seniority was already ruled out above -- this fail-open path
            # only concerns the coverage judgment, not the hard senior gate.)
            return Decision(notify=True, reason="no requirements extracted -- alerted unfiltered", **common)

        if coverage < self.settings.min_coverage_pct:
            return Decision(notify=False, reason=f"{coverage}% requirement coverage, below {self.settings.min_coverage_pct}%", **common)

        return Decision(notify=True, reason=f"{coverage}% requirement coverage", **common)

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
