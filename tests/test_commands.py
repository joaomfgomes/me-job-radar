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
    def __init__(self, last_poll_at=None, last_success_at=None, errors=None, empty=None,
                 core=None, secondary=None, add_result="added"):
        self._snapshot = {
            "last_poll_at": last_poll_at,
            "last_success_at": last_success_at,
            "consecutive_errors": errors or {},
            "consecutive_empty": empty or {},
        }
        self._core = core or []
        self._secondary = secondary or []
        self._add_result = add_result
        self.add_calls: list[str] = []

    def health_snapshot(self):
        return self._snapshot

    def current_searches(self):
        return self._core, self._secondary

    def validate_and_add_location(self, name):
        self.add_calls.append(name)
        return self._add_result


def make_settings(search_names):
    searches = [
        SearchConfig(name=n, keywords="AI Engineer", location="Remote") for n in search_names
    ]
    return Settings(searches=searches, telegram_bot_token="x", telegram_chat_id="y")


def _pipeline_for(search_names):
    core = [SearchConfig(name=n, keywords="AI Engineer", location="Remote") for n in search_names]
    return FakePipeline(core=core)


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
        core=settings.searches,
    )

    result = commands.format_status(store, pipeline, settings, started_at=time.time() - 3600, next_run_at=time.time() + 300)

    assert "Total jobs recorded: 1" in result
    assert "Search A: 1 jobs" in result
    assert "Search B: 0 jobs" in result
    assert "consecutive fetch errors" in result


def test_status_handles_never_polled(store):
    settings = make_settings(["Search A"])
    pipeline = FakePipeline(core=settings.searches)
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
    pipeline = FakePipeline(core=settings.searches)
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


# --- /rejected and /countries -------------------------------------------


def test_rejected_is_empty_before_anything_is_held_back(store):
    assert "Nothing has been held back" in commands.format_rejected(store)


def test_rejected_shows_coverage_and_reason(store):
    store.mark_seen(JobPosting("111", "AI Engineer", "Acme", "Lisbon", "url1", "1h ago", "Search A"))
    store.set_score("linkedin:111", 7, "70% requirement coverage, below 80%")
    store.set_deep_result("linkedin:111", 7, "reason", 70, "[]", True)

    result = commands.format_rejected(store)

    assert "AI Engineer" in result
    assert "70%" in result
    assert "below 80%" in result


def test_rejected_marks_jobs_that_never_reached_the_deep_pass(store):
    store.mark_seen(JobPosting("222", "Head of AI", "Acme", "Lisbon", "url2", "1h ago", "Search A"))
    store.set_score("linkedin:222", None, "prefilter: title contains dealbreaker term 'Head of'")

    assert "not scored" in commands.format_rejected(store)


def _pipeline_with_locations(*names):
    core = [
        SearchConfig(name=f"AI Engineer - {n}", keywords="AI Engineer", location=n, location_name=n)
        for n in names
    ]
    return FakePipeline(core=core)


def test_countries_lists_every_configured_location_as_active_by_default(store):
    result = commands.format_countries(store, _pipeline_with_locations("Germany", "Netherlands"))
    assert "✅ Germany" in result
    assert "✅ Netherlands" in result


def test_countries_off_then_shows_as_paused(store):
    pipeline = _pipeline_with_locations("Germany", "Netherlands")

    reply = commands.format_countries(store, pipeline, "off Germany")
    assert "paused" in reply

    listing = commands.format_countries(store, pipeline)
    assert "⏸ Germany" in listing
    assert "✅ Netherlands" in listing


def test_countries_toggle_is_case_insensitive(store):
    pipeline = _pipeline_with_locations("Netherlands")
    commands.format_countries(store, pipeline, "off netherlands")
    assert store.location_prefs() == {"Netherlands": False}


def test_countries_rejects_an_unknown_location(store):
    reply = commands.format_countries(store, _pipeline_with_locations("Germany"), "off Atlantis")
    assert "Unknown location" in reply
    assert store.location_prefs() == {}


def test_countries_rejects_a_bad_action(store):
    reply = commands.format_countries(store, _pipeline_with_locations("Germany"), "maybe Germany")
    assert "Usage:" in reply


def test_countries_add_delegates_to_the_pipelines_live_validator(store):
    """format_countries doesn't reimplement validation -- it's the
    pipeline's job (it needs live source instances), this just routes."""
    pipeline = _pipeline_with_locations("Germany")
    pipeline._add_result = "Validating 'Lisbon'...\n  ✅ linkedin: matched\n\nAdded 'Lisbon'."

    reply = commands.format_countries(store, pipeline, "add Lisbon")

    assert pipeline.add_calls == ["Lisbon"]
    assert "Added 'Lisbon'" in reply


def test_countries_add_with_no_name_does_not_call_the_validator(store):
    pipeline = _pipeline_with_locations("Germany")
    reply = commands.format_countries(store, pipeline, "add")
    assert pipeline.add_calls == []
    assert "Which location" in reply


def test_countries_remove_drops_a_custom_location(store):
    store.add_custom_location("Lisbon", {"linkedin": "Lisbon"})
    reply = commands.format_countries(store, _pipeline_with_locations("Germany"), "remove Lisbon")
    assert "Removed" in reply
    assert store.custom_locations() == {}


def test_countries_remove_redirects_config_locations_to_off(store):
    """A config.yaml location isn't in custom_locations -- removing it here
    would do nothing silently, so this must say so instead."""
    reply = commands.format_countries(store, _pipeline_with_locations("Germany"), "remove Germany")
    assert "/countries off" in reply


def test_countries_remove_unknown_name_says_so(store):
    reply = commands.format_countries(store, _pipeline_with_locations("Germany"), "remove Atlantis")
    assert "wasn't added" in reply


def test_dispatch_routes_the_new_commands(store):
    settings = make_settings(["Search A"])
    pipeline = _pipeline_with_locations("Germany")
    kwargs = dict(store=store, pipeline=pipeline, settings=settings, started_at=0, next_run_at=None)

    assert "held back" in commands.dispatch("/rejected", **kwargs)
    assert "Germany" in commands.dispatch("/countries", **kwargs)
    commands.dispatch("/countries off Germany", **kwargs)
    assert store.location_prefs() == {"Germany": False}
