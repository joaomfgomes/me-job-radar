"""Core data types shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JobPosting:
    """A single job posting, normalized regardless of source."""

    job_id: str  # stable id, unique within a source (e.g. LinkedIn's numeric urn id)
    title: str
    company: str
    location: str
    url: str
    posted_label: str  # human string as shown by the source, e.g. "20 hours ago"
    search_name: str  # which saved search found this job
    source: str = "linkedin"  # which board it came from; part of the dedup key
    # Some sources (Wellfound) return the full description in the search
    # response itself. When they do, the pipeline skips its separate JD fetch
    # entirely -- one less request, and the language/deep-scoring stages get
    # their text for free.
    description: str | None = None
    # Likewise, some sources state the experience range outright, which beats
    # asking a model to infer seniority from prose. None means "not stated".
    years_experience_min: int | None = None

    @property
    def dedup_key(self) -> str:
        """Job ids are only unique within a source, so the store keys on both."""
        return f"{self.source}:{self.job_id}"


@dataclass(frozen=True)
class SearchConfig:
    """One saved search from config.yaml."""

    name: str
    keywords: str
    location: str
    time_posted_seconds: int = 1800  # f_TPR window; how far back to ask for
    workplace_type: str | None = None  # f_WT: 1=on-site, 2=remote, 3=hybrid
    # How many 10-result pages to walk before giving up -- see
    # LinkedInGuestSource's module docstring. 3 covers 30 postings per
    # window, well above what a 30-minute window returns in practice.
    max_pages: int = 3
    # Which board runs this search, and which configured location it belongs
    # to. location_name is what /countries toggles on and off -- it maps a
    # generated search back to the country/city entry it came from, which the
    # free-text `location` query string can't reliably do.
    source: str = "linkedin"
    location_name: str | None = None

    # NOTE: there is deliberately no experience_levels/f_E field. The LinkedIn
    # guest endpoint accepts f_E and silently ignores it -- verified by diffing
    # results for the same search with and without it (identical 10 cards,
    # Senior/Staff titles included). Seniority filtering happens entirely
    # client-side: prefilter.py's title_contains_above_ceiling, the source's
    # own years_experience_min where it provides one, and the scorer's
    # is_junior_appropriate judgment on the full description.
