"""The poll cycle itself: seeding, isolation between searches, health
warnings and the daily digest. What gets alerted is covered separately in
test_pipeline_evaluation.py."""

import dataclasses

import pytest

from jobradar.evaluation import Evaluator
from jobradar.models import JobPosting, SearchConfig
from jobradar.pipeline import Pipeline
from jobradar.sources.base import RateLimitedError
from jobradar.store import Store
from tests.conftest import FakeNotifier, FakeSource


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_pipeline(settings, store, source):
    notifier = FakeNotifier()
    # No profile: every new job is alerted, which keeps these tests about
    # the cycle rather than about filtering.
    evaluator = Evaluator(None, None, None, deep_score_threshold=4, min_coverage_pct=80)
    return Pipeline(settings, store, {"linkedin": source}, notifier, evaluator), notifier


class FailingSource:
    def __init__(self, error):
        self._error = error

    def fetch(self, search):
        raise self._error


def test_first_run_of_a_search_records_jobs_without_alerting(store, settings, posting):
    pipeline, notifier = make_pipeline(settings, store, FakeSource([posting]))

    pipeline.run_cycle()

    assert notifier.sent == []
    assert store.total_count() == 1
    assert store.outcome_counts(days=1) == {"not_evaluated": 1}


def test_a_job_is_alerted_once_however_often_it_is_fetched(store, settings, posting):
    store.mark_seeded("Search A")
    pipeline, notifier = make_pipeline(settings, store, FakeSource([posting]))

    pipeline.run_cycle()
    pipeline.run_cycle()

    assert notifier.sent_postings == [posting]


def test_one_failing_search_does_not_stop_the_others(store, settings, posting):
    two = dataclasses.replace(settings, searches=[
        SearchConfig(name="Broken", keywords="k", location="l", source="missing-board"),
        SearchConfig(name="Search A", keywords="k", location="l"),
    ])
    store.mark_seeded("Search A")
    pipeline, notifier = make_pipeline(two, store, FakeSource([posting]))

    pipeline.run_cycle()

    assert notifier.sent_postings == [posting]


def test_repeated_fetch_failures_warn_once_at_the_threshold(store, settings):
    pipeline, notifier = make_pipeline(settings, store, FailingSource(RateLimitedError("429")))

    for _ in range(settings.consecutive_error_alert_threshold + 2):
        pipeline.run_cycle()

    assert len(notifier.texts) == 1 and "has failed 3 fetches in a row" in notifier.texts[0]
    assert pipeline.health_snapshot()["consecutive_errors"] == {"Search A": 5}


def test_a_success_resets_the_failure_count(store, settings, posting):
    pipeline, _ = make_pipeline(settings, store, FailingSource(RuntimeError("boom")))
    pipeline.run_cycle()
    pipeline.sources["linkedin"] = FakeSource([posting])

    pipeline.run_cycle()

    health = pipeline.health_snapshot()
    assert health["consecutive_errors"] == {"Search A": 0}
    assert health["last_success_at"] is not None


def test_persistently_empty_results_warn_that_the_parser_may_be_broken(store, settings):
    quick = dataclasses.replace(settings, consecutive_empty_alert_threshold=2)
    pipeline, notifier = make_pipeline(quick, store, FakeSource([]))

    pipeline.run_cycle()
    pipeline.run_cycle()

    assert len(notifier.texts) == 1 and "changed its markup" in notifier.texts[0]


def test_digest_sends_near_misses_once(store, settings):
    posting = JobPosting("7", "AI Engineer", "Acme", "Porto", "https://example.com/7", "1h", "Search A")
    store.mark_seen(posting)
    store.record_verdict(posting.dedup_key, outcome="low_coverage", reason="70%", notified=False, coverage_pct=70)
    pipeline, notifier = make_pipeline(settings, store, FakeSource([]))

    pipeline.send_digest()
    pipeline.send_digest()

    assert len(notifier.digests) == 1
    assert notifier.digests[0][0][0]["title"] == "AI Engineer"
