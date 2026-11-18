"""Store behaviour not already exercised through the pipeline tests: the
dedup key, verdict recording, the digest's selection, the rejected list,
and upgrading an older database file."""

import sqlite3

import pytest

from jobradar.models import JobPosting
from jobradar.store import Store, title_key


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def seed(store, job_id, source="linkedin", outcome="low_coverage", coverage=None, notified=False):
    posting = JobPosting(job_id=job_id, title=f"Job {job_id}", company="Acme", location="Porto",
                         url=f"https://x/{job_id}", posted_label="1h", search_name="s", source=source)
    store.mark_seen(posting)
    store.record_verdict(posting.dedup_key, outcome=outcome, reason="r", notified=notified, coverage_pct=coverage)
    return posting


def test_dedup_key_namespaces_by_source(store):
    seed(store, "1", source="linkedin")
    same_id_other_board = JobPosting("1", "T", "C", "L", "https://x", "1h", "s", source="wellfound")

    assert store.filter_unseen([same_id_other_board]) == [same_id_other_board]


def test_seeding_is_tracked_per_search(store):
    assert store.is_seeded("Search A") is False
    store.mark_seeded("Search A")
    assert store.is_seeded("Search A") is True and store.is_seeded("Search B") is False


def test_record_verdict_stores_requirements_as_json(store):
    posting = seed(store, "1")
    store.record_verdict(posting.dedup_key, outcome="alerted", reason="80%", notified=True, coverage_pct=80,
                         requirements=[{"requirement": "Python", "met": True, "evidence": "CV"}])

    row = store._conn.execute("SELECT outcome, notified, requirements_json FROM seen_jobs").fetchone()
    assert row == ("alerted", 1, '[{"requirement": "Python", "met": true, "evidence": "CV"}]')


def test_digest_takes_only_near_misses_inside_the_window(store):
    seed(store, "low", coverage=59)
    seed(store, "in", coverage=60)
    seed(store, "top", coverage=79)
    seed(store, "above", coverage=80)

    assert [r["title"] for r in store.digest_candidates(60, 80)] == ["Job top", "Job in"]


def test_digest_excludes_jobs_held_back_for_other_reasons(store):
    """A 70%-coverage job rejected for seniority or location is not a near
    miss: raising or lowering the coverage bar would not have changed it."""
    seed(store, "senior", outcome="not_junior", coverage=70)
    seed(store, "elsewhere", outcome="location", coverage=70)
    seed(store, "prefiltered", outcome="prefilter")

    assert store.digest_candidates(60, 80) == []


def test_a_job_is_digested_at_most_once(store):
    posting = seed(store, "1", coverage=70)
    store.mark_digest_sent([posting.dedup_key])
    store.mark_digest_sent([posting.dedup_key])

    assert store.digest_candidates(60, 80) == []


def test_rejected_jobs_are_newest_first_and_exclude_alerts(store):
    seed(store, "old", coverage=50)
    seed(store, "sent", outcome="alerted", coverage=90, notified=True)
    seed(store, "new", coverage=50)

    assert [r["title"] for r in store.rejected_jobs()] == ["Job new", "Job old"]
    assert len(store.rejected_jobs(limit=1)) == 1


def test_rejected_jobs_exclude_seeded_rows(store):
    """Rows recorded by first-run seeding were never evaluated, so they were
    not 'held back'."""
    store.mark_seen(JobPosting("9", "T", "C", "L", "https://x", "1h", "s"))
    assert store.rejected_jobs() == []


def test_an_older_database_is_upgraded_in_place(tmp_path):
    """A state.db from the first release has only the original columns."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE seen_jobs (job_id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL,
                    search_name TEXT NOT NULL, first_seen TEXT NOT NULL DEFAULT (datetime('now')))""")
    conn.execute("INSERT INTO seen_jobs (job_id, title, company, search_name) VALUES ('linkedin:1', 'T', 'C', 's')")
    conn.commit()
    conn.close()

    store = Store(path)
    store.record_verdict("linkedin:1", outcome="alerted", reason="r", notified=True)

    assert store.total_count() == 1
    assert store.outcome_counts(days=1) == {"alerted": 1}
    store.close()


@pytest.mark.parametrize("variant", [
    "AI Engineer", "ai engineer", "AI Engineer (m/f/d)", "AI Engineer [F/M/X]", "AI Engineer (f/m/div.)", "AI-Engineer",
])
def test_title_key_ignores_case_punctuation_and_gender_markers(variant):
    assert title_key(variant) == "ai engineer"


def test_title_key_keeps_words_that_change_the_job():
    assert title_key("Junior AI Engineer") != title_key("AI Engineer")


def test_find_repost_ignores_jobs_outside_the_window(store):
    old = seed(store, "1")
    store._conn.execute("UPDATE seen_jobs SET first_seen = datetime('now', '-40 days')")
    repost = JobPosting("2", old.title, old.company, "Porto", "https://x/2", "1h", "s")

    assert store.find_repost(repost, days=30) is None
    assert store.find_repost(repost, days=60)["outcome"] == "low_coverage"


def test_reposts_are_left_out_of_the_rejected_list(store):
    seed(store, "1")
    seed(store, "2", outcome="repost")
    assert [r["title"] for r in store.rejected_jobs()] == ["Job 1"]
