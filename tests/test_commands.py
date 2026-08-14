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
    searches = [
        SearchConfig(name=n, keywords="AI Engineer", location="Remote") for n in search_names
    ]
    return Settings(searches=searches, telegram_bot_token="x", telegram_chat_id="y")


def test_jobs_empty_db(store):
    result = commands.format_jobs(store)
    assert "No jobs recorded yet" in result


def test_jobs_lists_recent_entries(store):
    store.mark_seen(JobPosting("111", "AI Engineer", "Acme", "Lisbon", "url1", "1h ago", "Search A"))
    store.mark_seen(JobPosting("222", "ML Engineer", "Beta", "Remote", "url2", "2h ago", "Search B"))

    result = commands.format_jobs(store)
    assert "AI Engineer" in result
    assert "Acme" in result
    assert "ML Engineer" in result
    assert "linkedin.com/jobs/view/111" in result
    assert "linkedin.com/jobs/view/222" in result


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

    assert "Total jobs recorded: 1" in result
    assert "Search A: 1 jobs" in result
    assert "Search B: 0 jobs" in result
    assert "consecutive fetch errors" in result


def test_status_handles_never_polled(store):
    settings = make_settings(["Search A"])
    pipeline = FakePipeline()
    result = commands.format_status(store, pipeline, settings, started_at=time.time(), next_run_at=None)
    assert "Last poll attempt: never" in result
    assert "Next scheduled poll: unknown" in result


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
