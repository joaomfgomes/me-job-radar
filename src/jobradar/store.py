"""SQLite-backed dedup store.

One table, one job: remember which job IDs we've already alerted on.

First-run seeding: if the DB is empty when a search runs for the first time,
we mark whatever comes back as seen WITHOUT notifying. This makes the
database effectively disposable -- a lost/reset state.db just re-baselines
silently on the next run instead of blasting you with every job currently
in the time window.

Thread safety: the poll cycle (APScheduler worker thread) and the Telegram
command listener (its own thread, once added) both hit this store
concurrently. check_same_thread=False lets the same connection cross
threads, but SQLite connections still aren't safe for concurrent *access*
from multiple threads -- the RLock below serializes every public method so
a command handler's read can never interleave with a poll cycle's write.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from jobradar.models import JobPosting

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    job_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    search_name TEXT NOT NULL,
    first_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS seeded_searches (
    search_name TEXT PRIMARY KEY,
    seeded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Which configured locations are currently active. Only rows that exist
-- here override the config default (enabled); /countries writes here rather
-- than rewriting config.yaml, which would destroy its comments.
CREATE TABLE IF NOT EXISTS location_prefs (
    location_name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Locations added at runtime via /countries add, on top of whatever
-- config.yaml's search_matrix.locations already defines. queries_json maps
-- source name -> the query string that validated for that source (e.g.
-- {"linkedin": "Zürich"} if only LinkedIn validated -- see
-- location_validation.py for why the same city can validate on one board
-- and not another).
CREATE TABLE IF NOT EXISTS custom_locations (
    location_name TEXT PRIMARY KEY,
    queries_json TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False: the poll cycle and (once added) the command
        # listener each run on their own thread, neither of which constructs
        # Store. The RLock is what actually keeps access safe.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent ALTER TABLE additions -- safe on a fresh DB (no-op,
        columns already exist per SCHEMA... except these, added after the
        fact) and on an existing state.db with older rows."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(seen_jobs)")}
        additions = {
            "score": "ALTER TABLE seen_jobs ADD COLUMN score INTEGER",
            "score_reason": "ALTER TABLE seen_jobs ADD COLUMN score_reason TEXT",
            "notified": "ALTER TABLE seen_jobs ADD COLUMN notified INTEGER NOT NULL DEFAULT 0",
            # step 4: the cheap title-only pass, preserved even after deep
            # scoring overwrites score/score_reason with the JD-based result.
            # `score`/`score_reason` stay the "final, act-on-this" columns
            # throughout -- title_score is purely diagnostic.
            "title_score": "ALTER TABLE seen_jobs ADD COLUMN title_score INTEGER",
            "title_score_reason": "ALTER TABLE seen_jobs ADD COLUMN title_score_reason TEXT",
            # step 6 (enforcement): the deep pass's requirement-by-requirement
            # verdict. coverage_pct is computed in Python from requirements_json
            # (see scoring.DeepScoreResult) rather than asked of the model, so
            # it stays auditable -- requirements_json keeps the full list for
            # /rejected and the match brief in the alert.
            "coverage_pct": "ALTER TABLE seen_jobs ADD COLUMN coverage_pct INTEGER",
            "requirements_json": "ALTER TABLE seen_jobs ADD COLUMN requirements_json TEXT",
            "is_junior_appropriate": "ALTER TABLE seen_jobs ADD COLUMN is_junior_appropriate INTEGER",
            # Near-miss digest bookkeeping: a job belongs in at most one digest.
            "digest_sent": "ALTER TABLE seen_jobs ADD COLUMN digest_sent INTEGER NOT NULL DEFAULT 0",
            # Multi-source: which board a job came from, and its real URL.
            # The URL used to be reconstructed from job_id on the assumption
            # every job was a LinkedIn one -- that stopped being true.
            "source": "ALTER TABLE seen_jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'linkedin'",
            "url": "ALTER TABLE seen_jobs ADD COLUMN url TEXT",
        }
        for column, ddl in additions.items():
            if column not in existing:
                self._conn.execute(ddl)

        # Job ids are only unique *within* a board, so the primary key holds
        # JobPosting.dedup_key ("linkedin:123") rather than the bare id. Rows
        # written before multi-source existed are bare LinkedIn ids -- namespace
        # them once so old and new rows share one key format. Idempotent: the
        # LIKE guard means a second run matches nothing.
        self._conn.execute(
            "UPDATE seen_jobs SET job_id = 'linkedin:' || job_id WHERE job_id NOT LIKE '%:%'"
        )
        self._conn.execute(
            "UPDATE seen_jobs SET url = 'https://www.linkedin.com/jobs/view/' || "
            "substr(job_id, length('linkedin:') + 1) "
            "WHERE url IS NULL AND source = 'linkedin'"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def is_seeded(self, search_name: str) -> bool:
        """Has this search ever run before? Controls first-run seeding."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM seeded_searches WHERE search_name = ?", (search_name,)
            )
            return cur.fetchone() is not None

    def mark_seeded(self, search_name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seeded_searches (search_name) VALUES (?)",
                (search_name,),
            )
            self._conn.commit()

    def filter_unseen(self, postings: list[JobPosting]) -> list[JobPosting]:
        if not postings:
            return []
        with self._lock, closing(self._conn.cursor()) as cur:
            placeholders = ",".join("?" * len(postings))
            cur.execute(
                f"SELECT job_id FROM seen_jobs WHERE job_id IN ({placeholders})",
                [p.dedup_key for p in postings],
            )
            seen_ids = {row[0] for row in cur.fetchall()}
        return [p for p in postings if p.dedup_key not in seen_ids]

    def mark_seen(self, posting: JobPosting) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO seen_jobs (job_id, title, company, search_name, source, url)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (posting.dedup_key, posting.title, posting.company,
                 posting.search_name, posting.source, posting.url),
            )
            self._conn.commit()

    def set_notified(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE seen_jobs SET notified = 1 WHERE job_id = ?", (job_id,))
            self._conn.commit()

    def set_score(self, job_id: str, score: int | None, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE seen_jobs SET score = ?, score_reason = ? WHERE job_id = ?",
                (score, reason, job_id),
            )
            self._conn.commit()

    def set_title_score(self, job_id: str, score: int | None, reason: str) -> None:
        """Diagnostic record of the cheap title-only pass. Call set_score()
        separately for whatever should actually count as the job's final
        score (title score, or the deep score if the cascade ran)."""
        with self._lock:
            self._conn.execute(
                "UPDATE seen_jobs SET title_score = ?, title_score_reason = ? WHERE job_id = ?",
                (score, reason, job_id),
            )
            self._conn.commit()

    def set_deep_result(
        self,
        job_id: str,
        score: int | None,
        reason: str,
        coverage_pct: int | None,
        requirements_json: str,
        is_junior_appropriate: bool,
    ) -> None:
        """Records the deep pass's full verdict. set_score() is still the
        caller's job -- score/score_reason stay the 'final, act-on-this'
        columns, unchanged in meaning by this addition."""
        with self._lock:
            self._conn.execute(
                """UPDATE seen_jobs
                   SET coverage_pct = ?, requirements_json = ?, is_junior_appropriate = ?
                   WHERE job_id = ?""",
                (coverage_pct, requirements_json, int(is_junior_appropriate), job_id),
            )
            self._conn.commit()

    def digest_candidates(self, min_pct: int, max_pct: int) -> list[dict]:
        """Near-miss jobs awaiting their one and only digest: coverage in
        [min_pct, max_pct), never alerted, never digested before."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT job_id, title, company, search_name, coverage_pct, score_reason, url, source
                   FROM seen_jobs
                   WHERE digest_sent = 0 AND notified = 0
                     AND coverage_pct IS NOT NULL
                     AND coverage_pct >= ? AND coverage_pct < ?
                     AND is_junior_appropriate = 1
                   ORDER BY coverage_pct DESC""",
                (min_pct, max_pct),
            )
            rows = cur.fetchall()
        return [
            {"job_id": r[0], "title": r[1], "company": r[2], "search_name": r[3],
             "coverage_pct": r[4], "score_reason": r[5], "url": r[6], "source": r[7]}
            for r in rows
        ]

    def mark_digest_sent(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE seen_jobs SET digest_sent = 1 WHERE job_id = ?",
                [(job_id,) for job_id in job_ids],
            )
            self._conn.commit()

    def rejected_jobs(self, limit: int = 15) -> list[dict]:
        """Jobs that were scored but held back from alerting, newest first --
        the audit trail for an over-tight threshold."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT job_id, title, company, coverage_pct, score_reason, first_seen, url, source
                   FROM seen_jobs
                   WHERE notified = 0 AND score_reason IS NOT NULL
                   ORDER BY first_seen DESC LIMIT ?""",
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {"job_id": r[0], "title": r[1], "company": r[2], "coverage_pct": r[3],
             "score_reason": r[4], "first_seen": r[5], "url": r[6], "source": r[7]}
            for r in rows
        ]

    def recent_jobs(self, limit: int = 10) -> list[dict]:
        """Most recently seen jobs, newest first. Dicts (not JobPosting) since
        location/url were never persisted -- see url_for_job_id() below."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT job_id, title, company, search_name, first_seen, url, source
                   FROM seen_jobs ORDER BY first_seen DESC LIMIT ?""",
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "job_id": r[0],
                "title": r[1],
                "company": r[2],
                "search_name": r[3],
                "first_seen": r[4],
                "url": r[5],
                "source": r[6],
            }
            for r in rows
        ]

    def total_count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM seen_jobs")
            return cur.fetchone()[0]

    def counts_by_search(self) -> dict[str, int]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT search_name, COUNT(*) FROM seen_jobs GROUP BY search_name"
            )
            return dict(cur.fetchall())


    def custom_locations(self) -> dict[str, dict[str, str]]:
        """{location_name: {source: query}} for everything added via
        /countries add -- separate from config.yaml's own locations, which
        this store never touches."""
        with self._lock:
            cur = self._conn.execute("SELECT location_name, queries_json FROM custom_locations")
            return {name: json.loads(queries) for name, queries in cur.fetchall()}

    def add_custom_location(self, location_name: str, queries: dict[str, str]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO custom_locations (location_name, queries_json)
                   VALUES (?, ?)
                   ON CONFLICT(location_name) DO UPDATE SET queries_json = excluded.queries_json""",
                (location_name, json.dumps(queries)),
            )
            self._conn.commit()

    def remove_custom_location(self, location_name: str) -> bool:
        """Returns whether a row was actually removed -- lets the caller
        distinguish 'gone' from 'was never a custom location' (e.g. one of
        config.yaml's own, which this table never held)."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM custom_locations WHERE location_name = ?", (location_name,))
            self._conn.execute("DELETE FROM location_prefs WHERE location_name = ?", (location_name,))
            self._conn.commit()
            return cur.rowcount > 0

    def location_prefs(self) -> dict[str, bool]:
        """Only locations explicitly toggled appear here; anything absent is
        enabled by default, so a newly-added country works without a write."""
        with self._lock:
            cur = self._conn.execute("SELECT location_name, enabled FROM location_prefs")
            return {row[0]: bool(row[1]) for row in cur.fetchall()}

    def set_location_enabled(self, location_name: str, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO location_prefs (location_name, enabled, updated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(location_name) DO UPDATE
                     SET enabled = excluded.enabled, updated_at = excluded.updated_at""",
                (location_name, int(enabled)),
            )
            self._conn.commit()


def url_for_job_id(job_id: str) -> str:
    """LinkedIn's canonical job URL. seen_jobs never stored the original
    (tracking-param-laden) URL -- the job_id alone reconstructs a working one."""
    return f"https://www.linkedin.com/jobs/view/{job_id}"
