"""Offline tests for the evaluation cascade that decides what gets alerted.

Two properties matter throughout and are asserted repeatedly:
  * enforcement -- a job below the coverage bar, or not junior-appropriate,
    must never reach the notifier;
  * fail-open -- an infrastructure failure at ANY stage must let the job
    through rather than swallow it, and the decision must still carry
    whatever score was obtained before the failure.
"""

import pytest

from jobradar.models import JobPosting, SearchConfig
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
    pipeline = Pipeline(
        settings, store, {"linkedin": FakeSource([posting])}, notifier,
        profile=profile, scorer=scorer, jd_fetcher=jd_fetcher,
    )
    pipeline.run_cycle()
    return notifier, pipeline


def row(store, columns, job_id="linkedin:1"):
    return store._conn.execute(f"SELECT {columns} FROM seen_jobs WHERE job_id = ?", (job_id,)).fetchone()


# --- enforcement ---------------------------------------------------------

def test_coverage_at_or_above_threshold_alerts(store, settings, profile, posting):
    scorer = FakeScorer(ScoreResult(6, "Relevant."), make_deep(met=8, unmet=2, score=9))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent_postings == [posting]
    _, score, deep, language_unverified = notifier.sent[0]
    assert deep.coverage_pct == 80
    assert score == 9  # Decision.score prefers the deep pass
    assert language_unverified is False  # JD was available -- language WAS checked
    assert row(store, "notified, coverage_pct, is_junior_appropriate") == (1, 80, 1)


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


def test_jd_stated_years_above_ceiling_rejects_even_at_full_coverage(store, settings, profile, posting):
    """profile.max_years_experience=3 (see conftest) -- a JD stating 4+
    years must hard-reject regardless of how well the CV otherwise matches,
    exactly like the deterministic Wellfound-metadata check, but for a
    number the model extracted from free text instead of structured data."""
    scorer = FakeScorer(ScoreResult(9, "Relevant."), make_deep(met=10, unmet=0, years_required=4))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher("Full JD."))

    assert notifier.sent == []
    assert "4+ years" in row(store, "score_reason")[0]
    assert "3-year ceiling" in row(store, "score_reason")[0]


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
    scorer = FakeScorer(ScoreResult(9, "never reached"))
    notifier, _ = run(store, settings, profile, posting, scorer, FakeJDFetcher(dutch))

    assert notifier.sent == []
    assert scorer.score_calls == []
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
    assert "JD fetch failed" in row(store, "score_reason")[0]
    _, _, _, language_unverified = notifier.sent[0]
    assert language_unverified is True


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


def test_title_only_mode_alerts_regardless_of_title_score(store, settings, profile, posting):
    """No jd_fetcher and no source-provided description: there is nothing to
    check requirements against, so the job goes out rather than being judged
    on a title alone."""
    scorer = FakeScorer(ScoreResult(2, "Weak title."))
    notifier, _ = run(store, settings, profile, posting, scorer, jd_fetcher=None)

    assert notifier.sent_postings == [posting]
    assert scorer.deep_calls == []
    _, _, _, language_unverified = notifier.sent[0]
    assert language_unverified is True  # no description was ever available to check


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
    notifier.send_job = lambda posting, score=None, deep=None, language_unverified=False: None
    Pipeline(settings, store, {"linkedin": FakeSource([posting])}, notifier,
             profile=None, scorer=None).run_cycle()

    assert row(store, "job_id") is None  # not marked seen -> retried next cycle
