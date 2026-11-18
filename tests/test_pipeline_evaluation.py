"""Offline tests for the evaluation cascade that decides what gets alerted.

Two properties matter throughout and are asserted repeatedly:
  * enforcement -- a job below the coverage bar, or not junior-appropriate,
    must never reach the notifier;
  * fail-open -- an infrastructure failure at ANY stage must let the job
    through rather than swallow it, and the decision must still carry
    whatever score was obtained before the failure.
"""

import dataclasses

import pytest

from jobradar.evaluation import Evaluator
from jobradar.models import JobPosting
from jobradar.pipeline import Pipeline
from jobradar.profile import Dealbreakers, Profile
from jobradar.scoring import ScoreResult
from jobradar.store import Store
from tests.conftest import FakeJDFetcher, FakeNotifier, FakeScorer, FakeSource, make_deep


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def run(store, settings, profile, posting, scorer=None, jd_fetcher=None):
    store.mark_seeded(settings.searches[0].name)  # skip first-run seeding
    notifier = FakeNotifier()
    pipeline = Pipeline(settings, store, {"linkedin": FakeSource([posting])}, notifier,
                        make_evaluator(settings, profile, scorer, jd_fetcher))
    pipeline.run_cycle()
    return notifier, pipeline


def make_evaluator(settings, profile, scorer=None, jd_fetcher=None):
    return Evaluator(profile, scorer, jd_fetcher,
                     deep_score_threshold=settings.deep_score_threshold,
                     min_coverage_pct=settings.min_coverage_pct,
                     years_stretch=settings.years_stretch)


def row(store, columns, job_id="linkedin:1"):
    return store._conn.execute(f"SELECT {columns} FROM seen_jobs WHERE job_id = ?", (job_id,)).fetchone()


# --- enforcement ---------------------------------------------------------

def test_coverage_at_or_above_threshold_alerts(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(6, "Relevant."), make_deep(met=8, unmet=2, score=9))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent_postings == [posting]
    _, score, deep, warnings = notifier.sent[0]
    assert deep.coverage_pct == 80
    assert score == 9  # Decision.score prefers the deep pass
    assert warnings == ()  # a fully evaluated alert carries no warning
    assert row(store, "notified, coverage_pct, outcome") == (1, 80, "alerted")


def test_coverage_below_threshold_is_held_back(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(6, "Relevant."), make_deep(met=7, unmet=3))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent == []
    notified, coverage, reason = row(store, "notified, coverage_pct, score_reason")
    assert notified == 0
    assert coverage == 70  # recorded, so the digest can pick it up
    assert "below 80%" in reason


def test_senior_role_is_held_back_even_at_full_coverage(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=10, unmet=0, junior=False))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent == []
    assert "not junior-appropriate" in row(store, "score_reason")[0]


def test_jd_stated_years_beyond_the_stretch_rejects_even_at_full_coverage(store, settings, profile, posting):
    """Ceiling 3 (see conftest) plus a 1-year stretch: 5+ years is out,
    however well the CV otherwise matches -- a number the model extracted,
    compared in Python."""
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=10, unmet=0, years_required=5))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent == []
    assert row(store, "outcome, score_reason") == ("years", "posting requires 5+ years, above the 3-year ceiling")


def test_jd_stated_years_within_the_stretch_alert_with_a_warning(store, settings, profile, posting):
    """"4+ years" against a 3-year ceiling is a stretch worth applying to,
    so it goes out -- saying so."""
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=10, unmet=0, years_required=4))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    _, _, _, warnings = notifier.sent[0]
    assert warnings == ("Asks for 4+ years of experience (your ceiling is 3) — a stretch, still worth a try",)
    assert row(store, "outcome") == ("alerted",)


def test_no_stretch_means_the_ceiling_is_strict(store, settings, profile, posting):
    strict = dataclasses.replace(settings, years_stretch=0)
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=10, unmet=0, years_required=4))
    notifier, _ = run(store, strict, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent == []


def test_jd_stated_years_at_or_below_ceiling_does_not_reject(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=10, unmet=0, years_required=3))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent_postings == [posting]


def test_no_years_stated_falls_through_to_coverage_and_junior_checks(store, settings, profile, posting):
    """years_required=None (the model found no number in the JD) must not
    itself block anything -- absence of a stated number isn't evidence of
    anything, so the other checks still decide."""
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=10, unmet=0, years_required=None))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent_postings == [posting]


def test_title_score_below_deep_threshold_skips_the_expensive_pass(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(3, "Weak title."), make_deep())
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent == []
    assert scorer.deep_calls == []  # the whole point of the cheap first pass
    assert row(store, "title_score")[0] == 3


def test_prefilter_dealbreaker_stops_everything(store, settings, posting):
    profile = Profile(
        target_roles=["AI Engineer"], tech_stack=["Python"], seniority="entry-to-mid",
        max_years_experience=3, languages=["English"], adjacent_roles_require_llm_tie=[],
        dealbreakers=Dealbreakers(title_contains_above_ceiling=["AI Engineer"]), notes="",
    )
    scorer = FakeScorer(ScoreResult(9, "never reached"))
    jd = FakeJDFetcher("Full JD.")
    notifier, _ = run(store, settings, profile, posting, scorer, jd)

    assert notifier.sent == []
    assert scorer.score_calls == [] and jd.calls == []  # no spend on a certainty


def test_stated_years_above_ceiling_rejects_without_any_llm_call(store, settings, profile):
    """Wellfound states yearsExperienceMin outright -- a fact, so no model
    should be consulted about it."""
    senior = JobPosting(
        job_id="9", title="AI Engineer", company="Acme", location="Amsterdam",
        url="https://wellfound.com/jobs/9", posted_label="1h ago", search_name="Search A",
        source="wellfound", description="Long description.", years_experience_min=6,
    )
    scorer = FakeScorer(ScoreResult(9, "never reached"))
    notifier, _ = run(store, settings, profile, senior, scorer, FakeJDFetcher("unused"))

    assert notifier.sent == []
    assert scorer.score_calls == []
    assert "6+ years" in row(store, "score_reason", "wellfound:9")[0]


def test_language_mismatch_is_held_back(store, settings, profile, posting):
    dutch = (
        "Wij zoeken een gedreven Software Engineer om ons team te versterken. In deze "
        "functie werk je aan de ontwikkeling van innovatieve softwareoplossingen binnen "
        "een agile team. Je hebt ervaring met Python en bent bekend met moderne "
        "cloudtechnologieen. Je werkt nauw samen met collega's uit verschillende "
        "disciplines om onze producten naar een hoger niveau te tillen."
    )
    scorer = FakeScorer(ScoreResult(9, "Relevant title."))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher(dutch))

    assert notifier.sent == []
    assert scorer.deep_calls == []  # rejected before the deep pass was paid for
    assert "language" in row(store, "score_reason")[0]


# --- fail-open -----------------------------------------------------------

def test_jd_fetch_failure_alerts_unfiltered(store, settings, profile, posting):
    """This is the exact failure mode that let a Dutch-only posting through
    once in production (a transient JD-fetch 429 skipped the language check
    entirely) -- the job still alerts (fail-open), but must be flagged as
    language-unverified rather than looking like a normally-screened job."""
    scorer = FakeScorer(ScoreResult(9, "never reached"))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher(None))

    assert notifier.sent_postings == [posting]
    assert "description unavailable" in row(store, "score_reason")[0]
    _, _, _, warnings = notifier.sent[0]
    assert "Not filtered — description unavailable" in warnings[0]


def test_title_scoring_failure_alerts_unfiltered(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(None, "scoring unavailable: timeout", unscored=True))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent_postings == [posting]
    assert scorer.deep_calls == []


def test_deep_scoring_failure_alerts_and_keeps_the_title_score(store, settings, profile, posting):
    """Decision.score must fall back to the title pass rather than going
    None just because the deep call failed."""
    scorer = FakeScorer(ScoreResult(6, "Good title."), make_deep(unscored=True, score=None))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent_postings == [posting]
    _, score, _, _ = notifier.sent[0]
    assert score == 6
    assert row(store, "score, title_score") == (6, 6)


def test_empty_requirement_list_alerts_rather_than_reading_as_zero_percent(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=0, unmet=0))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent_postings == [posting]
    assert "no requirements extracted" in row(store, "score_reason")[0]


def test_no_profile_or_scorer_alerts_everything(store, settings, posting):
    notifier, _ = run(store, settings, None, posting, None, None)
    assert notifier.sent_postings == [posting]


def test_no_description_available_alerts_on_the_title_score_alone(store, settings, profile, posting):
    """No fetcher and no source-provided description: there is nothing to
    check requirements against, so a relevant-looking title goes out,
    flagged as unverified."""
    scorer = FakeScorer(ScoreResult(8, "Relevant title."))
    notifier, _ = run(store, settings, profile, posting, scorer, jd_fetcher=None)

    assert notifier.sent_postings == [posting]
    assert scorer.deep_calls == []
    _, _, _, warnings = notifier.sent[0]
    assert "Not filtered — description unavailable" in warnings[0]
    assert row(store, "outcome") == ("alerted_unfiltered",)


def test_title_gate_runs_before_the_description_is_fetched(store, settings, profile, posting):
    """The whole point of the ordering: an irrelevant title must not cost a
    LinkedIn request."""
    jd = FakeJDFetcher("Full JD.")
    scorer = FakeScorer(ScoreResult(2, "Weak title."), make_deep())
    notifier, _ = run(store, settings, profile, posting, scorer, jd)

    assert notifier.sent == []
    assert jd.calls == []
    assert row(store, "outcome") == ("title_low",)


def test_prefilter_still_runs_when_scoring_is_unavailable(store, settings, posting):
    """Deterministic rules need no model, so a missing API key must not
    switch them off."""
    strict = Profile(
        target_roles=["AI Engineer"], tech_stack=["Python"], seniority="entry-to-mid",
        max_years_experience=3, languages=["English"], adjacent_roles_require_llm_tie=[],
        dealbreakers=Dealbreakers(title_contains=["AI Engineer"]), notes="",
    )
    notifier, _ = run(store, settings, strict, posting, scorer=None, jd_fetcher=None)

    assert notifier.sent == []
    assert row(store, "outcome") == ("prefilter",)


def test_location_mismatch_is_held_back_even_at_full_coverage(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=10, unmet=0, location_fit=False))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent == []
    assert row(store, "outcome") == ("location",)


def test_alert_records_chance_and_market_data(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=9, unmet=1, chance=72))
    run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert row(store, "outcome, interview_chance_pct") == ("alerted", 72)
    assert store.insights_count(days=1) == 1
    assert {s["name"] for s in store.skill_demand(days=1)} == {"Python", "Kubernetes", "LangChain"}


def test_held_back_jobs_still_feed_the_market_data(store, settings, profile, posting):
    """A job rejected for coverage was still read in full -- what it asks
    for is exactly the kind of gap the market view exists to show."""
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=2, unmet=8))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent == []
    assert store.insights_count(days=1) == 1


def test_fail_open_alert_carries_the_reason_to_the_notifier(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(None, "scoring unavailable: HTTP 401", unscored=True))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert "Not filtered — title scoring failed" in notifier.sent[0][3][0]


# --- source-provided descriptions ---------------------------------------

def test_source_description_is_used_instead_of_fetching(store, settings, profile):
    rich = JobPosting(
        job_id="7", title="AI Engineer", company="Acme", location="Amsterdam",
        url="https://wellfound.com/jobs/7", posted_label="1h ago", search_name="Search A",
        source="wellfound", description="A long English description about RAG and LangGraph work.",
    )
    jd = FakeJDFetcher("SHOULD NOT BE USED")
    scorer = FakeScorer(ScoreResult(8, "Relevant."), make_deep(met=9, unmet=1))
    notifier, _ = run(store, settings, profile, rich, scorer, jd)

    assert jd.calls == []  # no second request for text we already had
    assert scorer.deep_calls[0][1] == rich.description
    assert notifier.sent_postings == [rich]


def test_notify_failure_leaves_job_unseen_for_retry(store, settings, profile, posting):
    store.mark_seeded(settings.searches[0].name)
    notifier = FakeNotifier()
    notifier.send_job = lambda posting, **kwargs: None
    Pipeline(settings, store, {"linkedin": FakeSource([posting])}, notifier,
             make_evaluator(settings, profile=None)).run_cycle()

    assert row(store, "job_id") is None  # not marked seen -> retried next cycle


# --- reposts --------------------------------------------------------------

def repost_of(posting, job_id="2", title=None):
    return dataclasses.replace(posting, job_id=job_id, title=title or posting.title)


def run_both(store, settings, profile, first, second, scorer):
    """Evaluates `first`, then `second` in a later cycle."""
    store.mark_seeded(settings.searches[0].name)
    notifier = FakeNotifier()
    source = FakeSource([first])
    pipeline = Pipeline(settings, store, {"linkedin": source}, notifier,
                        make_evaluator(settings, profile, scorer, FakeJDFetcher("Full JD.")))
    pipeline.run_cycle()
    source._postings = [second]
    pipeline.run_cycle()
    return notifier


def test_a_repost_inherits_the_earlier_rejection_without_a_new_evaluation(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=2, unmet=8))
    notifier = run_both(store, settings, profile, posting, repost_of(posting, title="AI ENGINEER (m/f/d)"), scorer)

    assert notifier.sent == []
    assert len(scorer.deep_calls) == 1  # the repost cost nothing
    outcome, reason = row(store, "outcome, score_reason", job_id="linkedin:2")
    assert outcome == "repost" and "low_coverage" in reason


def test_a_repost_of_an_alerted_job_is_not_alerted_again(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=9, unmet=1))
    notifier = run_both(store, settings, profile, posting, repost_of(posting), scorer)

    assert notifier.sent_postings == [posting]
    assert "(alerted)" in row(store, "score_reason", job_id="linkedin:2")[0]


def test_a_repost_of_an_unfiltered_job_gets_a_real_evaluation(store, settings, profile, posting):
    """The first copy went out unfiltered (the model was down), so it was
    never really judged -- the repost is its second chance."""
    scorer = FakeScorer(ScoreResult(None, "scoring unavailable", unscored=True))
    notifier = run_both(store, settings, profile, posting, repost_of(posting), scorer)

    assert len(scorer.score_calls) == 2
    assert len(notifier.sent) == 2


def test_the_same_title_at_another_company_is_not_a_repost(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=9, unmet=1))
    other = dataclasses.replace(posting, job_id="2", company="Other Co")
    notifier = run_both(store, settings, profile, posting, other, scorer)

    assert notifier.sent_postings == [posting, other]
