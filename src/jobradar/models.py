"""Core data types shared across the app."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JobPosting:
    """A single job posting, normalised regardless of source."""

    job_id: str  # stable id, unique within a source (e.g. LinkedIn's numeric id)
    title: str
    company: str
    location: str
    url: str
    posted_label: str  # human string as shown by the source, e.g. "20 hours ago"
    search_name: str  # which search found this job
    source: str = "linkedin"
    # Some sources (Wellfound) return the full description with the search
    # result. When they do, the evaluation skips its separate fetch.
    description: str | None = None
    # Likewise, some sources state the experience floor outright, which beats
    # asking a model to infer it from prose. None means "not stated".
    years_experience_min: int | None = None

    @property
    def dedup_key(self) -> str:
        """Job ids are only unique within a source, so the store keys on both."""
        return f"{self.source}:{self.job_id}"


@dataclass(frozen=True)
class SearchConfig:
    """One search: a keyword in a location on one board."""

    name: str
    keywords: str
    location: str  # in the board's own format: free text on LinkedIn, a URL slug on Wellfound
    source: str = "linkedin"
    time_posted_seconds: int = 3600  # how far back to ask for
    max_pages: int = 3  # LinkedIn returns 10 results per page

    # There are deliberately no seniority or remote/on-site fields. LinkedIn's
    # guest endpoint accepts those filters (f_E, f_WT) and ignores them --
    # verified by comparing results with and without each. Both are decided
    # from the posting itself: prefilter.py for seniority words in the title,
    # and the deep pass (scoring.py) for seniority and location fit.
