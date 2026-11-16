"""Decides what happens to one posting: alert it, or hold it back.

A posting is fully evaluated BEFORE anyone is notified, by a cascade that
runs its cheapest checks first. Each stage either rejects the job or hands
it to the next one:

  #  stage              cost                  rejects
  1  prefilter          free                  title/employer dealbreakers,
                                              seniority words in the title
  2  stated years       free                  boards that state an experience
                                              floor above the ceiling
  3  title score        1 small LLM call      roles that are plainly off-target
  4  description        1 request (LinkedIn)  nothing -- see "fail open"
  5  language           free                  descriptions the candidate
                                              cannot read
  6  deep score         1 LLM call            too many years, not
                                              junior-appropriate, wrong
                                              location, low CV coverage

The title score runs before the description is fetched on purpose: it is
what keeps the number of LinkedIn requests low. Only postings whose title
looks relevant cost a description fetch.

FAIL OPEN. Every stage that depends on something outside this process (the
description fetch, either LLM call) answers "alert it" when that thing
fails. An outage must never silently swallow a real job; the alert says it
was not filtered so the noise is at least labelled.

This module holds no I/O of its own -- the scorer and the description
fetcher are passed in -- so the whole cascade is tested offline
(tests/test_pipeline_evaluation.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from jobradar.cv_ingest import format_cv_context
from jobradar.lang_filter import detect_language_mismatch
from jobradar.models import JobPosting
from jobradar.prefilter import prefilter
from jobradar.profile import Profile
from jobradar.scoring import DeepScoreResult, JobScorer
from jobradar.sources.linkedin_jd import JobDescriptionFetcher


class Outcome(StrEnum):
    """Where a posting's evaluation ended. Stored per job (seen_jobs.outcome)
    and counted by /stats to show the shape of the funnel."""

    ALERTED = "alerted"                # passed every gate
    UNFILTERED = "alerted_unfiltered"  # a fail-open path: sent without a full evaluation
    PREFILTER = "prefilter"
    YEARS = "years"                    # asks for more experience than the ceiling
    TITLE_LOW = "title_low"
    LANGUAGE = "language"
    NOT_JUNIOR = "not_junior"
    LOCATION = "location"
    LOW_COVERAGE = "low_coverage"


@dataclass(frozen=True)
class Decision:
    """The verdict on one posting. `reason` explains it either way, and is
    what gets stored and shown for jobs that were held back."""

    outcome: Outcome
    reason: str
    deep: DeepScoreResult | None = None
    title_score: int | None = None
    title_score_reason: str | None = None
    # True when no description was ever available, so the language check
    # never ran. The alert warns about it explicitly.
    language_unverified: bool = False

    @property
    def notify(self) -> bool:
        return self.outcome in (Outcome.ALERTED, Outcome.UNFILTERED)

    @property
    def score(self) -> int | None:
        """The score to act on: the deep pass's if it produced one, else the
        title pass's, so a job that slipped through on an API error still
        carries whatever judgment was obtained."""
        if self.deep is not None and not self.deep.unscored:
            return self.deep.score
        return self.title_score


@dataclass(frozen=True)
class Thresholds:
    """The tunable bars, lifted out of Settings so the evaluator depends on
    three numbers rather than on the whole configuration."""

    deep_score_threshold: int
    min_coverage_pct: int


class Evaluator:
    def __init__(
        self,
        profile: Profile | None,
        scorer: JobScorer | None,
        jd_fetcher: JobDescriptionFetcher | None,
        thresholds: Thresholds,
    ) -> None:
        self._profile = profile
        self._scorer = scorer
        self._jd_fetcher = jd_fetcher
        self._thresholds = thresholds

    def evaluate(self, posting: JobPosting) -> Decision:
        profile = self._profile
        if profile is None:
            return Decision(Outcome.UNFILTERED, "no profile.yaml -- filtering disabled")

        # 1-2: deterministic gates. They need no model, so they run even
        # when scoring is unavailable.
        pre = prefilter(posting, profile)
        if not pre.passed:
            return Decision(Outcome.PREFILTER, f"prefilter: {pre.reason}")

        ceiling = profile.max_years_experience
        stated = posting.years_experience_min
        if ceiling is not None and stated is not None and stated > ceiling:
            return Decision(Outcome.YEARS, f"posting requires {stated}+ years, above the {ceiling}-year ceiling")

        if self._scorer is None:
            return Decision(Outcome.UNFILTERED, "scoring disabled -- prefilter only")

        # 3: title score.
        title = self._scorer.score(posting, profile)
        if title.unscored:
            return Decision(
                Outcome.UNFILTERED,
                f"title scoring failed ({title.reason})",
                title_score_reason=title.reason,
            )
        scores = {"title_score": title.score, "title_score_reason": title.reason}
        if title.score < self._thresholds.deep_score_threshold:
            return Decision(Outcome.TITLE_LOW, f"title score {title.score}/10: {title.reason}", **scores)

        # 4: description -- free when the board already supplied it.
        jd_text = posting.description
        if jd_text is None and self._jd_fetcher is not None:
            jd_text = self._jd_fetcher.fetch(posting.job_id)
        if jd_text is None:
            return Decision(
                Outcome.UNFILTERED, "description unavailable -- title score only",
                language_unverified=True, **scores,
            )

        # 5: language.
        mismatch = detect_language_mismatch(jd_text, profile.languages)
        if mismatch:
            return Decision(Outcome.LANGUAGE, f"language: {mismatch}", **scores)

        # 6: deep score.
        deep = self._scorer.score_deep(
            posting, profile, jd_text,
            title_score=title.score,
            title_score_reason=title.reason,
            cv_context=format_cv_context(),  # None when profile.generated.yaml is absent
        )
        return self._judge(deep, scores)

    def _judge(self, deep: DeepScoreResult, scores: dict) -> Decision:
        """Applies the gates to a deep result, strictest signal first."""
        if deep.unscored:
            return Decision(Outcome.UNFILTERED, f"deep scoring failed ({deep.reason})", deep=deep, **scores)

        # An extracted number, compared in Python: "4+ years" means 4+ years,
        # whatever the model thinks of the candidate otherwise.
        ceiling = self._profile.max_years_experience
        if deep.years_required is not None and ceiling is not None and deep.years_required > ceiling:
            return Decision(
                Outcome.YEARS,
                f"posting requires {deep.years_required}+ years, above the {ceiling}-year ceiling",
                deep=deep, **scores,
            )

        # The model's holistic read, for postings that are senior without
        # stating a number.
        if not deep.is_junior_appropriate:
            return Decision(Outcome.NOT_JUNIOR, f"not junior-appropriate: {deep.reason}", deep=deep, **scores)

        if not deep.location_fit:
            mode = deep.insights.work_mode if deep.insights else "unspecified"
            return Decision(Outcome.LOCATION, f"location does not fit (work mode: {mode})", deep=deep, **scores)

        coverage = deep.coverage_pct
        if coverage is None:
            # No requirements extracted. Reading that as 0% would reject on
            # a modelling artefact, so fail open.
            return Decision(Outcome.UNFILTERED, "no requirements extracted", deep=deep, **scores)

        bar = self._thresholds.min_coverage_pct
        if coverage < bar:
            return Decision(Outcome.LOW_COVERAGE, f"{coverage}% requirement coverage, below {bar}%", deep=deep, **scores)
        return Decision(Outcome.ALERTED, f"{coverage}% requirement coverage", deep=deep, **scores)
