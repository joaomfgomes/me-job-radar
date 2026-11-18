"""Offline tests for command handlers -- no network, no scheduler, no Telegram."""

import time

import pytest

from jobradar import commands
from jobradar.config import Settings
from jobradar.models import JobPosting, SearchConfig
from jobradar.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class FakePipeline:
    def __init__(self, last_poll_at=None, last_success_at=None, errors=None, empty=None):
        self._snapshot = {
            "last_poll_at": last_poll_at,
            "last_success_at": last_success_at,
            "consecutive_errors": errors or {},
            "consecutive_empty": empty or {},
        }

    def health_snapshot(self):
        return self._snapshot


def make_settings(search_names):
    searches = [SearchConfig(name=n, keywords="AI Engineer", location="Porto") for n in search_names]
    return Settings(searches=searches, telegram_bot_token="x", telegram_chat_id="y")


def test_jobs_empty_db(store):
    result = commands.format_jobs(store)
    assert "No jobs recorded yet" in result


def test_jobs_lists_recent_entries(store):
    store.mark_seen(JobPosting("111", "AI Engineer", "Acme", "Lisbon", "https://example.com/url1", "1h ago", "Search A"))
    store.mark_seen(JobPosting("222", "ML Engineer", "Beta", "Remote", "https://example.com/url2", "2h ago", "Search B"))

    result = commands.format_jobs(store)
    assert "AI Engineer" in result
    assert "Acme" in result
    assert "ML Engineer" in result
    assert "url1" in result  # the stored URL, not one rebuilt from job_id
    assert "url2" in result


def test_jobs_respects_limit(store):
    for i in range(15):
        store.mark_seen(JobPosting(str(i), f"Job {i}", "Co", "Loc", "url", "now", "Search"))
    result = commands.format_jobs(store, limit=5)
    assert "Last 5 job(s)" in result


def test_status_reports_counts_and_health(store):
    store.mark_seen(JobPosting("111", "AI Engineer", "Acme", "Lisbon", "url1", "1h ago", "Search A"))
    settings = make_settings(["Search A", "Search B"])
    pipeline = FakePipeline(
        last_poll_at=time.time() - 120,
        last_success_at=time.time() - 130,
        errors={"Search B": 4},
    )

    result = commands.format_status(store, pipeline, settings, started_at=time.time() - 3600, next_run_at=time.time() + 300)

    assert "Jobs recorded: 1" in result
    assert "Search A: 1 jobs" in result
    assert "Search B: 0 jobs" in result
    assert "4 failed fetches in a row" in result


def test_status_handles_never_polled(store):
    settings = make_settings(["Search A"])
    pipeline = FakePipeline()
    result = commands.format_status(store, pipeline, settings, started_at=time.time(), next_run_at=None)
    assert "Last poll: never" in result
    assert "Next poll: unknown" in result


def test_help_lists_commands():
    result = commands.format_help()
    assert "/jobs" in result
    assert "/status" in result
    assert "/help" in result


def test_dispatch_routes_known_commands(store):
    settings = make_settings(["Search A"])
    pipeline = FakePipeline()
    kwargs = dict(store=store, pipeline=pipeline, settings=settings, started_at=time.time(), next_run_at=None)

    assert "No jobs recorded" in commands.dispatch("/jobs", **kwargs)
    assert "job-radar status" in commands.dispatch("/status", **kwargs)
    assert "job-radar commands" in commands.dispatch("/help", **kwargs)
    assert "job-radar commands" in commands.dispatch("/start", **kwargs)


def test_dispatch_handles_group_chat_command_suffix(store):
    settings = make_settings(["Search A"])
    pipeline = FakePipeline()
    kwargs = dict(store=store, pipeline=pipeline, settings=settings, started_at=time.time(), next_run_at=None)
    assert "job-radar commands" in commands.dispatch("/help@meu_job_radar_bot", **kwargs)


def test_dispatch_unknown_command(store):
    settings = make_settings(["Search A"])
    pipeline = FakePipeline()
    kwargs = dict(store=store, pipeline=pipeline, settings=settings, started_at=time.time(), next_run_at=None)
    result = commands.dispatch("/banana", **kwargs)
    assert "Unknown command" in result


def test_dispatch_empty_text(store):
    settings = make_settings(["Search A"])
    pipeline = FakePipeline()
    kwargs = dict(store=store, pipeline=pipeline, settings=settings, started_at=time.time(), next_run_at=None)
    result = commands.dispatch("", **kwargs)
    assert "Unknown command" in result


# --- /rejected ------------------------------------------------------------


def test_rejected_is_empty_before_anything_is_held_back(store):
    assert "Nothing has been held back" in commands.format_rejected(store)


def test_rejected_shows_coverage_and_reason(store):
    store.mark_seen(JobPosting("111", "AI Engineer", "Acme", "Porto", "url1", "1h ago", "Search A"))
    store.record_verdict("linkedin:111", outcome="low_coverage", notified=False, coverage_pct=70,
                         reason="70% requirement coverage, below 80%")

    result = commands.format_rejected(store)

    assert "AI Engineer" in result
    assert "70%" in result
    assert "below 80%" in result


def test_rejected_marks_jobs_that_never_reached_the_deep_pass(store):
    store.mark_seen(JobPosting("222", "Head of AI", "Acme", "Porto", "url2", "1h ago", "Search A"))
    store.record_verdict("linkedin:222", outcome="prefilter", notified=False,
                         reason="prefilter: title contains dealbreaker term 'Head of'")

    assert "not scored" in commands.format_rejected(store)


def test_removed_commands_are_reported_as_unknown(store):
    kwargs = dict(store=store, pipeline=FakePipeline(), settings=make_settings(["Search A"]),
                  started_at=time.time(), next_run_at=None)
    assert "Unknown command" in commands.dispatch("/countries", **kwargs)
