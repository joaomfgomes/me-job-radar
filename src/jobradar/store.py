"""SQLite-backed dedup store.

One table, one job: remember which job IDs we've already alerted on.

First-run seeding: if the DB is empty when a search runs for the first time,
we mark whatever comes back as seen WITHOUT notifying. This makes the
database effectively disposable -- a lost/reset state.db just re-baselines
silently on the next run instead of blasting you with every job currently
in the time window.
"""

from __future__ import annotations

import sqlite3
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
"""


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: APScheduler runs the poll cycle in a worker
        # thread, not the thread that constructs Store. Safe here because
        # max_instances=1 on the scheduled job means calls are never concurrent.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def is_seeded(self, search_name: str) -> bool:
        """Has this search ever run before? Controls first-run seeding."""
        cur = self._conn.execute(
            "SELECT 1 FROM seeded_searches WHERE search_name = ?", (search_name,)
        )
        return cur.fetchone() is not None

    def mark_seeded(self, search_name: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seeded_searches (search_name) VALUES (?)",
            (search_name,),
        )
        self._conn.commit()

    def filter_unseen(self, postings: list[JobPosting]) -> list[JobPosting]:
        if not postings:
            return []
        with closing(self._conn.cursor()) as cur:
            placeholders = ",".join("?" * len(postings))
            cur.execute(
                f"SELECT job_id FROM seen_jobs WHERE job_id IN ({placeholders})",
                [p.job_id for p in postings],
            )
            seen_ids = {row[0] for row in cur.fetchall()}
        return [p for p in postings if p.job_id not in seen_ids]

    def mark_seen(self, posting: JobPosting) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO seen_jobs (job_id, title, company, search_name)
               VALUES (?, ?, ?, ?)""",
            (posting.job_id, posting.title, posting.company, posting.search_name),
        )
        self._conn.commit()
