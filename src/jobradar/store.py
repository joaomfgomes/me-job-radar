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
            self._conn.commit()

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
                [p.job_id for p in postings],
            )
            seen_ids = {row[0] for row in cur.fetchall()}
        return [p for p in postings if p.job_id not in seen_ids]

    def mark_seen(self, posting: JobPosting) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO seen_jobs (job_id, title, company, search_name)
                   VALUES (?, ?, ?, ?)""",
                (posting.job_id, posting.title, posting.company, posting.search_name),
            )
            self._conn.commit()

    def recent_jobs(self, limit: int = 10) -> list[dict]:
        """Most recently seen jobs, newest first. Dicts (not JobPosting) since
        location/url were never persisted -- see url_for_job_id() below."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT job_id, title, company, search_name, first_seen
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


def url_for_job_id(job_id: str) -> str:
    """LinkedIn's canonical job URL. seen_jobs never stored the original
    (tracking-param-laden) URL -- the job_id alone reconstructs a working one."""
    return f"https://www.linkedin.com/jobs/view/{job_id}"
