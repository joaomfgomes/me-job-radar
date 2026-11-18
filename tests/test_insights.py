"""Statistics and market data: the store's aggregates and the three
commands that present them (/stats, /market, /gaps)."""

import pytest

from jobradar.commands import dispatch, format_gaps, format_market, format_stats
from jobradar.models import JobPosting
from jobradar.scoring import JobInsights, Skill
from jobradar.store import Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "state.db")
    yield store
    store.close()


def add_job(store, job_id, outcome, *, coverage=None, chance=None, notified=False, insights=None):
    posting = JobPosting(
        job_id=job_id, title=f"AI Engineer {job_id}", company="Acme", location="Porto",
        url=f"https://example.com/{job_id}", posted_label="1h ago", search_name="Search A",
    )
    store.mark_seen(posting)
    store.record_verdict(posting.dedup_key, outcome=outcome, reason="why", notified=notified,
                         coverage_pct=coverage, interview_chance_pct=chance)
    if insights is not None:
        store.save_insights(posting.dedup_key, insights)


def insights(*skills, work_mode="remote", salary=None):
    low, high = salary or (None, None)
    return JobInsights(
        role_family="AI Engineer", seniority_level="junior", work_mode=work_mode, contract_type="permanent",
        salary_min=low, salary_max=high, salary_currency="EUR" if salary else None,
        skills=tuple(Skill(name, "tool", required, has) for name, required, has in skills),
    )


@pytest.fixture
def populated(store):
    add_job(store, "1", "alerted", coverage=90, chance=70, notified=True,
            insights=insights(("Python", True, True), ("Kubernetes", True, False), salary=(40000, 50000)))
    add_job(store, "2", "low_coverage", coverage=50, chance=20,
            insights=insights(("python", True, True), ("Kubernetes", False, False), ("Terraform", True, False),
                              work_mode="hybrid"))
    add_job(store, "3", "prefilter")
    add_job(store, "4", "title_low")
    return store


def test_outcome_counts_groups_by_where_evaluation_ended(populated):
    assert populated.outcome_counts(days=7) == {"alerted": 1, "low_coverage": 1, "prefilter": 1, "title_low": 1}


def test_unevaluated_rows_are_counted_separately(store):
    store.mark_seen(JobPosting("9", "T", "C", "L", "https://example.com/9", "1h", "Search A"))
    assert store.outcome_counts(days=7) == {"not_evaluated": 1}


def test_match_averages_cover_only_deep_scored_jobs(populated):
    averages = populated.match_averages(days=7)
    assert averages["scored"] == 2
    assert averages["avg_coverage"] == 70 and averages["avg_chance"] == 45


def test_skill_demand_merges_spellings_and_ranks_by_postings(populated):
    demand = populated.skill_demand(days=7)

    assert [row["jobs"] for row in demand] == [2, 2, 1]
    python = next(row for row in demand if row["name"].lower() == "python")
    assert python["candidate_has"] is True and python["required_in"] == 2
    kubernetes = next(row for row in demand if row["name"] == "Kubernetes")
    assert kubernetes["required_in"] == 1  # asked for twice, required once


def test_skill_gaps_are_the_skills_the_cv_does_not_show(populated):
    assert [row["name"] for row in populated.skill_demand(days=7, only_missing=True)] == ["Kubernetes", "Terraform"]


def test_saving_insights_twice_does_not_double_count(populated):
    populated.save_insights("linkedin:1", insights(("Python", True, True)))

    assert populated.insights_count(days=7) == 2
    assert next(r for r in populated.skill_demand(days=7) if r["name"] == "Kubernetes")["jobs"] == 1


def test_distribution_rejects_unknown_columns(populated):
    assert dict(populated.insight_distribution("work_mode", days=7)) == {"remote": 1, "hybrid": 1}
    with pytest.raises(ValueError):
        populated.insight_distribution("job_id; DROP TABLE seen_jobs", days=7)


def test_salary_summary_ignores_postings_that_state_none(populated):
    assert populated.salary_summary(days=7) == [{"currency": "EUR", "jobs": 1, "avg_min": 40000, "avg_max": 50000}]


# --- commands -------------------------------------------------------------

def test_stats_shows_the_funnel_and_best_chances(populated):
    text = format_stats(populated, "7")

    assert "Last 7 day(s)" in text and "4 jobs seen, 4 evaluated" in text
    assert "✅ Alerted: <b>1</b> (25%)" in text
    assert "Low CV coverage: <b>1</b>" in text
    assert "Average CV coverage: 70%" in text
    assert "~70%" in text and "AI Engineer 1" in text


def test_stats_warns_when_most_alerts_were_unfiltered(store):
    add_job(store, "1", "alerted_unfiltered", notified=True)
    assert "scoring was unavailable" in format_stats(store)


def test_stats_with_no_jobs(store):
    assert "No jobs recorded" in format_stats(store)


def test_market_lists_skills_with_cv_marks(populated):
    text = format_market(populated, "")

    assert "based on 2 postings" in text
    assert "❌ Kubernetes — 2 jobs (100%), required in 1" in text
    assert "<b>Work mode:</b>" in text and "remote 1" in text
    assert "Salary (EUR)" in text and "40,000–50,000" in text


def test_market_without_data_explains_why(store):
    assert "No market data" in format_market(store)


def test_gaps_lists_only_missing_skills(populated):
    text = format_gaps(populated, "30")
    assert "Kubernetes" in text and "Terraform" in text
    assert "Python" not in text and "python" not in text


def test_bad_period_argument_falls_back_to_the_default(populated):
    assert "Last 30 day(s)" in format_stats(populated, "abc")
    assert "Last 365 day(s)" in format_stats(populated, "99999")


def test_dispatch_routes_the_new_commands(populated):
    kwargs = dict(store=populated, pipeline=None, settings=None, started_at=0.0, next_run_at=None)
    assert "jobs seen" in dispatch("/stats 7", **kwargs)
    assert "Market" in dispatch("/market", **kwargs)
    assert "Skill gaps" in dispatch("/gaps@my_bot 14", **kwargs)
