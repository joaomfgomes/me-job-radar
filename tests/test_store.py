"""Direct tests for Store methods not already exercised through the
pipeline evaluation tests: digest candidate selection, rejection listing,
location preferences, and the multi-source dedup key.
"""

import pytest

from jobradar.models import JobPosting
from jobradar.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def seed(store, job_id, source="linkedin", coverage=None, junior=True, notified=False, digest_sent=False):
    p = JobPosting(job_id=job_id, title=f"Job {job_id}", company="Acme", location="Remote",
                    url=f"https://x/{job_id}", posted_label="1h", search_name="s", source=source)
    store.mark_seen(p)
    if coverage is not None:
        store.set_deep_result(p.dedup_key, score=5, reason="r", coverage_pct=coverage,
                              requirements_json="[]", is_junior_appropriate=junior)
        store.set_score(p.dedup_key, 5, "r")
    if notified:
        store.set_notified(p.dedup_key)
    if digest_sent:
        store.mark_digest_sent([p.dedup_key])
    return p


def test_dedup_key_namespaces_by_source(store):
    """The same numeric id from two different boards must not collide."""
    a = seed(store, "1", source="linkedin")
    b = seed(store, "1", source="wellfound")
    assert a.dedup_key != b.dedup_key
    assert store._conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0] == 2


def test_digest_candidates_respects_the_percentage_window(store):
    seed(store, "1", coverage=55)   # below the window
    seed(store, "2", coverage=60)   # window's lower bound, inclusive
    seed(store, "3", coverage=79)   # window's upper bound, exclusive of max
    seed(store, "4", coverage=80)   # at/above min_coverage_pct -- would have been alerted, not a near-miss

    rows = store.digest_candidates(min_pct=60, max_pct=80)
    assert {r["job_id"].split(":")[-1] for r in rows} == {"2", "3"}


def test_digest_candidates_excludes_notified_and_already_digested(store):
    seed(store, "1", coverage=65, notified=True)     # already alerted
    seed(store, "2", coverage=65, digest_sent=True)   # already digested once
    seed(store, "3", coverage=65)                     # the only real candidate

    rows = store.digest_candidates(min_pct=60, max_pct=80)
    assert [r["job_id"].split(":")[-1] for r in rows] == ["3"]


def test_digest_candidates_excludes_senior_inappropriate_near_misses(store):
    seed(store, "1", coverage=65, junior=False)
    seed(store, "2", coverage=65, junior=True)
    rows = store.digest_candidates(min_pct=60, max_pct=80)
    assert [r["job_id"].split(":")[-1] for r in rows] == ["2"]


def test_digest_candidates_excludes_jobs_that_never_reached_the_deep_pass(store):
    seed(store, "1")  # no coverage_pct at all -- e.g. prefiltered
    assert store.digest_candidates(min_pct=0, max_pct=100) == []


def test_mark_digest_sent_is_idempotent(store):
    p = seed(store, "1", coverage=65)
    store.mark_digest_sent([p.dedup_key])
    store.mark_digest_sent([p.dedup_key])  # must not raise
    assert store.digest_candidates(min_pct=60, max_pct=80) == []


def test_rejected_jobs_respects_the_limit(store):
    for i in range(5):
        seed(store, str(i), coverage=50)
    assert len(store.rejected_jobs(limit=2)) == 2
    assert len(store.rejected_jobs(limit=10)) == 5


def test_rejected_jobs_orders_newest_first(store):
    import time
    seed(store, "0", coverage=50)
    store._conn.execute("UPDATE seen_jobs SET first_seen = '2020-01-01 00:00:00'")
    seed(store, "1", coverage=50)  # inserted after, with a real (later) timestamp
    rows = store.rejected_jobs(limit=10)
    assert rows[0]["job_id"] == "linkedin:1"


def test_rejected_jobs_excludes_alerted_jobs(store):
    seed(store, "1", coverage=90, notified=True)
    assert store.rejected_jobs() == []


def test_location_prefs_defaults_to_absent_meaning_enabled(store):
    assert store.location_prefs() == {}  # nothing toggled -> caller treats absence as enabled


def test_set_location_enabled_is_idempotent_and_updates_in_place(store):
    store.set_location_enabled("Germany", False)
    store.set_location_enabled("Germany", True)
    assert store.location_prefs() == {"Germany": True}


# --- custom_locations (runtime-added via /countries add) -----------------


def test_custom_locations_starts_empty(store):
    assert store.custom_locations() == {}


def test_add_custom_location_persists_per_source_queries(store):
    store.add_custom_location("Lisbon", {"linkedin": "Lisbon", "wellfound": "lisbon"})
    assert store.custom_locations() == {"Lisbon": {"linkedin": "Lisbon", "wellfound": "lisbon"}}


def test_add_custom_location_is_upsert(store):
    store.add_custom_location("Lisbon", {"linkedin": "Lisbon"})
    store.add_custom_location("Lisbon", {"linkedin": "Lisbon", "wellfound": "lisbon"})
    assert store.custom_locations() == {"Lisbon": {"linkedin": "Lisbon", "wellfound": "lisbon"}}


def test_remove_custom_location_reports_whether_it_existed(store):
    store.add_custom_location("Lisbon", {"linkedin": "Lisbon"})
    assert store.remove_custom_location("Lisbon") is True
    assert store.remove_custom_location("Lisbon") is False
    assert store.custom_locations() == {}


def test_remove_custom_location_also_clears_its_prefs(store):
    """Otherwise a removed-then-re-added location could silently inherit a
    stale on/off state from before it was removed."""
    store.add_custom_location("Lisbon", {"linkedin": "Lisbon"})
    store.set_location_enabled("Lisbon", False)
    store.remove_custom_location("Lisbon")
    assert store.location_prefs() == {}
