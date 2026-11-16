"""Offline tests for runtime location management: dynamic search
regeneration (a location added via /countries add is searched on the very
next cycle, no restart) and the rotation-safety warning that guards against
reintroducing the coverage-gap bug found earlier (a full secondary rotation
taking longer than a search's own time window).
"""

import pytest

from jobradar.config import Settings
from jobradar.models import JobPosting, SearchConfig
from jobradar.pipeline import Pipeline
from jobradar.store import Store
from tests.conftest import FakeNotifier


class FakeValidatingSource:
    """fetch() returns postings whose location matches whatever was asked,
    so validate_location always passes -- used to test the wiring between
    Pipeline.validate_and_add_location and the store, not the validator
    itself (that's test_location_validation.py's job)."""

    def __init__(self, timeout: float = 1.0):
        pass

    def fetch(self, search: SearchConfig):
        return [
            JobPosting(job_id=str(i), title="AI Engineer", company="Acme",
                       location=f"{search.location}, Somewhere", url="https://x",
                       posted_label="1h", search_name=search.name)
            for i in range(5)
        ]

    def close(self):
        pass


class FakeRejectingSource(FakeValidatingSource):
    """Every query returns results from an unrelated place -- as the real
    endpoints do for a location they don't recognize."""

    def fetch(self, search: SearchConfig):
        return [
            JobPosting(job_id=str(i), title="AI Engineer", company="Acme",
                       location="Nowhere Relevant", url="https://x",
                       posted_label="1h", search_name=search.name)
            for i in range(5)
        ]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_settings(**overrides):
    base = dict(
        searches=[], telegram_bot_token="x", telegram_chat_id="y",
        secondary_batch_size=2,
        matrix_locations=[{"name": "Madrid", "linkedin": "Madrid"}],
        matrix_sources={"linkedin": {"core_keywords": ["AI Engineer"],
                                     "secondary_keywords": ["LLM Engineer"],
                                     "defaults": {"time_posted_seconds": 1800}}},
    )
    base.update(overrides)
    return Settings(**base)


def make_pipeline(store, settings, source_cls=FakeValidatingSource):
    return Pipeline(settings, store, {"linkedin": source_cls()}, FakeNotifier())


# --- dynamic regeneration --------------------------------------------------


def test_a_location_added_via_the_store_is_searched_without_restart(store):
    """Simulates what /countries add does under the hood: write to the
    store, then the very next current_searches() call picks it up."""
    settings = make_settings()
    pipeline = make_pipeline(store, settings)

    before, _ = pipeline.current_searches()
    assert {s.location_name for s in before} == {"Madrid"}

    store.add_custom_location("Lisbon", {"linkedin": "Lisbon"})

    after, _ = pipeline.current_searches()
    assert {s.location_name for s in after} == {"Madrid", "Lisbon"}


def test_removed_custom_location_disappears_from_the_next_cycle(store):
    settings = make_settings()
    pipeline = make_pipeline(store, settings)
    store.add_custom_location("Lisbon", {"linkedin": "Lisbon"})
    assert "Lisbon" in {s.location_name for s in pipeline.current_searches()[0]}

    store.remove_custom_location("Lisbon")

    core, _ = pipeline.current_searches()
    assert "Lisbon" not in {s.location_name for s in core}


def test_disabled_custom_location_is_skipped_by_run_cycle(store):
    settings = make_settings()
    pipeline = make_pipeline(store, settings)
    store.add_custom_location("Lisbon", {"linkedin": "Lisbon"})
    store.set_location_enabled("Lisbon", False)

    store.mark_seeded("AI Engineer - Madrid (linkedin)")
    pipeline.run_cycle()

    rows = store._conn.execute("SELECT DISTINCT search_name FROM seen_jobs").fetchall()
    assert not any("Lisbon" in r[0] for r in rows)


def test_hand_written_searches_still_run_alongside_the_matrix(store):
    settings = make_settings(searches=[SearchConfig(name="Custom Search", keywords="k", location="l")])
    pipeline = make_pipeline(store, settings)
    core, _ = pipeline.current_searches()
    assert "Custom Search" in {s.name for s in core}
    assert any(s.location_name == "Madrid" for s in core)


# --- validate_and_add_location ---------------------------------------------


def test_validate_and_add_location_persists_on_success(store):
    settings = make_settings()
    pipeline = make_pipeline(store, settings, FakeValidatingSource)

    report = pipeline.validate_and_add_location("Lisbon")

    assert "✅" in report
    assert "Added" in report
    assert store.custom_locations() == {"Lisbon": {"linkedin": "Lisbon"}}


def test_validate_and_add_location_does_not_persist_on_failure(store):
    settings = make_settings()
    pipeline = make_pipeline(store, settings, FakeRejectingSource)

    report = pipeline.validate_and_add_location("Atlantis")

    assert "❌" in report
    assert "Not added" in report
    assert store.custom_locations() == {}


def test_validate_and_add_location_with_no_matrix_sources_configured(store):
    settings = make_settings(matrix_sources={})
    pipeline = make_pipeline(store, settings)
    report = pipeline.validate_and_add_location("Lisbon")
    assert "nothing to validate" in report.lower()
    assert store.custom_locations() == {}


# --- rotation-safety warning -----------------------------------------------


def test_rotation_warning_present_when_pool_outgrows_its_window(store):
    """3 secondary searches at batch=1, each with only a 5-minute window --
    a full rotation (3 cycles) at any real poll interval blows past that."""
    settings = make_settings(
        secondary_batch_size=1,
        matrix_locations=[
            {"name": "A", "linkedin": "A"}, {"name": "B", "linkedin": "B"}, {"name": "C", "linkedin": "C"},
        ],
        matrix_sources={"linkedin": {"secondary_keywords": ["LLM Engineer"],
                                     "defaults": {"time_posted_seconds": 300}}},
        poll_interval_minutes=10,
    )
    pipeline = make_pipeline(store, settings)
    warning = pipeline._rotation_warning()
    assert "⚠️" in warning


def test_rotation_warning_absent_when_window_comfortably_covers_the_rotation(store):
    settings = make_settings(
        secondary_batch_size=3,
        matrix_locations=[{"name": "A", "linkedin": "A"}],
        matrix_sources={"linkedin": {"secondary_keywords": ["LLM Engineer"],
                                     "defaults": {"time_posted_seconds": 7200}}},
        poll_interval_minutes=10,
    )
    pipeline = make_pipeline(store, settings)
    warning = pipeline._rotation_warning()
    assert "⚠️" not in warning


def test_rotation_warning_is_appended_to_the_add_location_report(store):
    settings = make_settings(
        secondary_batch_size=1,
        matrix_locations=[{"name": "A", "linkedin": "A"}, {"name": "B", "linkedin": "B"}],
        matrix_sources={"linkedin": {"core_keywords": ["AI Engineer"], "secondary_keywords": ["LLM Engineer"],
                                     "defaults": {"time_posted_seconds": 60}}},
        poll_interval_minutes=10,
    )
    pipeline = make_pipeline(store, settings, FakeValidatingSource)
    report = pipeline.validate_and_add_location("Lisbon")
    assert "⚠️" in report
