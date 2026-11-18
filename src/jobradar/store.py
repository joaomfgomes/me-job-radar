"""SQLite-backed state: everything the app remembers between runs.

Tables:
  seen_jobs        one row per posting ever fetched -- the dedup record,
                   plus the verdict the evaluation reached for it
  job_insights     one row per posting the deep pass read: role, seniority,
                   work mode, salary (the market database)
  job_skills       one row per skill a posting asked for, with whether the
                   CV shows it (what /market and /gaps aggregate)
  seeded_searches  which searches have had their first, silent run

First-run seeding: the first time a search runs, whatever it returns is
recorded WITHOUT alerting. That makes a new search -- or a lost state.db --
re-baseline quietly instead of sending every job currently in the window.

Thread safety: the poll cycle (scheduler thread) and the Telegram listener
(main thread) share one connection. check_same_thread=False lets it cross
threads, and the RLock serialises every public method so a command's read
can never interleave with a cycle's write.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

from jobradar.models import JobPosting

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    job_id TEXT PRIMARY KEY,              -- JobPosting.dedup_key, e.g. "linkedin:123"
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    search_name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'linkedin',
    url TEXT,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    -- The verdict (see record_verdict). All NULL for rows that were never
    -- evaluated, i.e. the ones recorded by first-run seeding.
    outcome TEXT,                         -- evaluation.Outcome value
    score_reason TEXT,                    -- why, in words
    notified INTEGER NOT NULL DEFAULT 0,
    score INTEGER,                        -- deep score, else title score
    title_score INTEGER,
    title_score_reason TEXT,
    coverage_pct INTEGER,                 -- computed from requirements_json
    requirements_json TEXT,               -- [{requirement, met, evidence}]
    interview_chance_pct INTEGER,         -- model estimate; never a gate
    chance_reason TEXT,
    digest_sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_seen_jobs_first_seen ON seen_jobs (first_seen);

CREATE TABLE IF NOT EXISTS seeded_searches (
    search_name TEXT PRIMARY KEY,
    seeded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS job_insights (
    job_id TEXT PRIMARY KEY,
    role_family TEXT,
    seniority_level TEXT,
    work_mode TEXT,
    contract_type TEXT,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT,
    extracted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- skill_key is the lower-cased name, so "PyTorch" and "pytorch" count as
-- one skill; `name` keeps a spelling to display.
CREATE TABLE IF NOT EXISTS job_skills (
    job_id TEXT NOT NULL,
    skill_key TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    required INTEGER NOT NULL,
    candidate_has INTEGER NOT NULL,
    PRIMARY KEY (job_id, skill_key)
);
CREATE INDEX IF NOT EXISTS idx_job_skills_key ON job_skills (skill_key);
"""

# Columns added to seen_jobs after the first release. A state.db created by
# an older version gets them added on startup; a new one already has them.
_ADDED_COLUMNS = {
    "source": "TEXT NOT NULL DEFAULT 'linkedin'",
    "url": "TEXT",
    "outcome": "TEXT",
    "score_reason": "TEXT",
    "notified": "INTEGER NOT NULL DEFAULT 0",
    "score": "INTEGER",
    "title_score": "INTEGER",
    "title_score_reason": "TEXT",
    "coverage_pct": "INTEGER",
    "requirements_json": "TEXT",
    "interview_chance_pct": "INTEGER",
    "chance_reason": "TEXT",
    "digest_sent": "INTEGER NOT NULL DEFAULT 0",
}

_DISTRIBUTION_COLUMNS = ("role_family", "seniority_level", "work_mode", "contract_type")


# Gender and contract markers that recruiters add or drop between reposts of
# the same job: "(m/f/d)", "[F/M/X]", "(f/m/div.)", "(h/m)".
_TITLE_NOISE = re.compile(r"[\(\[]\s*[mfwhdx]\s*/\s*[mfwhdx][^\)\]]*[\)\]]", re.IGNORECASE)


def title_key(title: str) -> str:
    """A title reduced to what identifies the job: lower case, without
    gender markers or punctuation. "AI Engineer (m/f/d)" and "AI engineer"
    give the same key."""
    title = _TITLE_NOISE.sub(" ", title.lower())
    return " ".join(re.sub(r"[^\w]+", " ", title).split())


def _since(days: int) -> str:
    """SQLite datetime modifier for 'the last N days'."""
    return f"-{int(days)} days"


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._add_missing_columns()
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _add_missing_columns(self) -> None:
        """Brings an older state.db up to the current seen_jobs layout.
        Runs before SCHEMA so its indexes can rely on every column."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(seen_jobs)")}
        if not existing:
            return  # fresh database -- SCHEMA creates the table whole
        for column, definition in _ADDED_COLUMNS.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE seen_jobs ADD COLUMN {column} {definition}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Dedup and seeding
    # ------------------------------------------------------------------

    def is_seeded(self, search_name: str) -> bool:
        """Has this search ever run before? Controls first-run seeding."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seeded_searches WHERE search_name = ?", (search_name,)
            ).fetchone()
        return row is not None

    def mark_seeded(self, search_name: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO seeded_searches (search_name) VALUES (?)", (search_name,))
            self._conn.commit()

    def filter_unseen(self, postings: list[JobPosting]) -> list[JobPosting]:
        if not postings:
            return []
        placeholders = ",".join("?" * len(postings))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT job_id FROM seen_jobs WHERE job_id IN ({placeholders})",
                [p.dedup_key for p in postings],
            ).fetchall()
        seen = {row[0] for row in rows}
        return [p for p in postings if p.dedup_key not in seen]

    def mark_seen(self, posting: JobPosting) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO seen_jobs (job_id, title, company, search_name, source, url)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (posting.dedup_key, posting.title, posting.company,
                 posting.search_name, posting.source, posting.url),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Verdicts
    # ------------------------------------------------------------------

    def record_verdict(
        self,
        job_id: str,
        *,
        outcome: str,
        reason: str,
        notified: bool,
        score: int | None = None,
        title_score: int | None = None,
        title_score_reason: str | None = None,
        coverage_pct: int | None = None,
        requirements: list[dict] | None = None,
        interview_chance_pct: int | None = None,
        chance_reason: str | None = None,
    ) -> None:
        """Stores everything the evaluation determined about a job, in one
        write. Fields the cascade never reached stay NULL."""
        with self._lock:
            self._conn.execute(
                """UPDATE seen_jobs
                   SET outcome = ?, score_reason = ?, notified = ?, score = ?,
                       title_score = ?, title_score_reason = ?,
                       coverage_pct = ?, requirements_json = ?,
                       interview_chance_pct = ?, chance_reason = ?
                   WHERE job_id = ?""",
                (outcome, reason, int(notified), score, title_score, title_score_reason,
                 coverage_pct, json.dumps(requirements) if requirements is not None else None,
                 interview_chance_pct, chance_reason, job_id),
            )
            self._conn.commit()

    def find_repost(self, posting: JobPosting, days: int) -> dict | None:
        """The most recent job from the same company with the same title
        (see title_key) whose evaluation reached a verdict in the last
        `days` days, or None.

        Recruiters republish the same job under a new id every few days.
        Evaluating each copy again costs requests and model calls, and --
        because a model's judgment varies slightly between runs -- can turn
        an earlier "no" into a "yes" by chance. Unfiltered verdicts are
        skipped: those jobs were never really evaluated, so a repost is
        their second chance."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT title, outcome, score_reason, notified
                   FROM seen_jobs
                   WHERE lower(company) = lower(?) AND job_id != ?
                     AND outcome IS NOT NULL AND outcome NOT IN ('alerted_unfiltered', 'repost')
                     AND first_seen >= datetime('now', ?)
                   ORDER BY first_seen DESC, rowid DESC""",
                (posting.company, posting.dedup_key, _since(days)),
            ).fetchall()
        key = title_key(posting.title)
        for title, outcome, reason, notified in rows:
            if title_key(title) == key:
                return {"outcome": outcome, "reason": reason or "", "notified": bool(notified)}
        return None

    def rejected_jobs(self, limit: int = 15) -> list[dict]:
        """Jobs that were evaluated and held back, newest first -- the audit
        trail for the filters. Reposts are left out: their verdict is the
        original's, already listed."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT title, company, coverage_pct, score_reason, url
                   FROM seen_jobs
                   WHERE notified = 0 AND outcome IS NOT NULL AND outcome != 'repost'
                   ORDER BY first_seen DESC, rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {"title": r[0], "company": r[1], "coverage_pct": r[2], "score_reason": r[3], "url": r[4]}
            for r in rows
        ]

    def recent_jobs(self, limit: int = 10) -> list[dict]:
        """Most recently seen jobs, newest first."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT title, company, search_name, first_seen, url
                   FROM seen_jobs ORDER BY first_seen DESC, rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {"title": r[0], "company": r[1], "search_name": r[2], "first_seen": r[3], "url": r[4]}
            for r in rows
        ]

    def digest_candidates(self, min_pct: int, max_pct: int) -> list[dict]:
        """Near misses awaiting their one digest: held back for coverage
        alone, with coverage in [min_pct, max_pct), never digested before."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT job_id, title, company, coverage_pct, url, source
                   FROM seen_jobs
                   WHERE outcome = 'low_coverage' AND digest_sent = 0
                     AND coverage_pct >= ? AND coverage_pct < ?
                   ORDER BY coverage_pct DESC""",
                (min_pct, max_pct),
            ).fetchall()
        return [
            {"job_id": r[0], "title": r[1], "company": r[2], "coverage_pct": r[3], "url": r[4], "source": r[5]}
            for r in rows
        ]

    def mark_digest_sent(self, job_ids: list[str]) -> None:
        with self._lock:
            self._conn.executemany(
                "UPDATE seen_jobs SET digest_sent = 1 WHERE job_id = ?", [(job_id,) for job_id in job_ids]
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Statistics: how the funnel behaved over a period
    # ------------------------------------------------------------------

    def total_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]

    def counts_by_search(self) -> dict[str, int]:
        with self._lock:
            return dict(self._conn.execute("SELECT search_name, COUNT(*) FROM seen_jobs GROUP BY search_name"))

    def outcome_counts(self, days: int) -> dict[str, int]:
        """{outcome: jobs} for postings first seen in the last `days` days.
        Rows that were never evaluated are counted under 'not_evaluated'."""
        with self._lock:
            return dict(self._conn.execute(
                """SELECT COALESCE(outcome, 'not_evaluated'), COUNT(*)
                   FROM seen_jobs WHERE first_seen >= datetime('now', ?)
                   GROUP BY 1""",
                (_since(days),),
            ))

    def match_averages(self, days: int) -> dict:
        """Averages over the postings the deep pass scored in the period."""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*), AVG(coverage_pct), AVG(interview_chance_pct)
                   FROM seen_jobs
                   WHERE first_seen >= datetime('now', ?) AND coverage_pct IS NOT NULL""",
                (_since(days),),
            ).fetchone()
        return {"scored": row[0], "avg_coverage": row[1], "avg_chance": row[2]}

    def best_matches(self, days: int, limit: int = 5) -> list[dict]:
        """Alerted jobs with the highest estimated interview chance."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT title, company, url, coverage_pct, interview_chance_pct
                   FROM seen_jobs
                   WHERE first_seen >= datetime('now', ?) AND notified = 1
                     AND interview_chance_pct IS NOT NULL
                   ORDER BY interview_chance_pct DESC, coverage_pct DESC LIMIT ?""",
                (_since(days), limit),
            ).fetchall()
        return [
            {"title": r[0], "company": r[1], "url": r[2], "coverage_pct": r[3], "interview_chance_pct": r[4]}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Market data: what postings ask for
    # ------------------------------------------------------------------

    def save_insights(self, job_id: str, insights) -> None:
        """Stores a scoring.JobInsights. Replaces any earlier extraction for
        the same job, so re-scoring never double-counts a skill."""
        skills: dict[str, tuple] = {}
        for skill in insights.skills:
            key = " ".join(skill.name.lower().split())
            if key and key not in skills:
                skills[key] = (job_id, key, skill.name, skill.category, int(skill.required), int(skill.candidate_has))
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO job_insights
                   (job_id, role_family, seniority_level, work_mode, contract_type,
                    salary_min, salary_max, salary_currency)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, insights.role_family, insights.seniority_level, insights.work_mode,
                 insights.contract_type, insights.salary_min, insights.salary_max, insights.salary_currency),
            )
            self._conn.execute("DELETE FROM job_skills WHERE job_id = ?", (job_id,))
            self._conn.executemany("INSERT INTO job_skills VALUES (?, ?, ?, ?, ?, ?)", list(skills.values()))
            self._conn.commit()

    def insights_count(self, days: int) -> int:
        """How many postings the market figures for the period are based on."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM job_insights WHERE extracted_at >= datetime('now', ?)", (_since(days),)
            ).fetchone()[0]

    def skill_demand(self, days: int, limit: int = 15, only_missing: bool = False) -> list[dict]:
        """Most requested skills, by number of postings asking for them.
        `only_missing` restricts it to skills the CV does not show -- the
        gap list."""
        having = "HAVING MAX(s.candidate_has) = 0" if only_missing else ""
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT MAX(s.name), MAX(s.category), COUNT(*), SUM(s.required), MAX(s.candidate_has)
                    FROM job_skills s JOIN job_insights i ON i.job_id = s.job_id
                    WHERE i.extracted_at >= datetime('now', ?)
                    GROUP BY s.skill_key {having}
                    ORDER BY COUNT(*) DESC, SUM(s.required) DESC, s.skill_key LIMIT ?""",
                (_since(days), limit),
            ).fetchall()
        return [
            {"name": r[0], "category": r[1], "jobs": r[2], "required_in": r[3], "candidate_has": bool(r[4])}
            for r in rows
        ]

    def insight_distribution(self, column: str, days: int, limit: int = 6) -> list[tuple[str, int]]:
        """[(value, jobs)] for one categorical column of job_insights."""
        if column not in _DISTRIBUTION_COLUMNS:
            raise ValueError(f"not a distribution column: {column}")
        with self._lock:
            return self._conn.execute(
                f"""SELECT COALESCE(NULLIF({column}, ''), 'unspecified'), COUNT(*)
                    FROM job_insights WHERE extracted_at >= datetime('now', ?)
                    GROUP BY 1 ORDER BY 2 DESC LIMIT ?""",
                (_since(days), limit),
            ).fetchall()

    def salary_summary(self, days: int) -> list[dict]:
        """Stated salary ranges, per currency. Most postings state none, so
        `jobs` matters as much as the averages."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT salary_currency, COUNT(*), AVG(salary_min), AVG(salary_max)
                   FROM job_insights
                   WHERE extracted_at >= datetime('now', ?)
                     AND salary_currency IS NOT NULL AND (salary_min IS NOT NULL OR salary_max IS NOT NULL)
                   GROUP BY salary_currency ORDER BY 2 DESC""",
                (_since(days),),
            ).fetchall()
        return [{"currency": r[0], "jobs": r[1], "avg_min": r[2], "avg_max": r[3]} for r in rows]
