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


@dataclass(frozen=True)
class SearchConfig:
    """One saved search from config.yaml."""

    name: str
    keywords: str
    location: str
    time_posted_seconds: int = 1800  # f_TPR window; how far back to ask for
    workplace_type: str | None = None  # f_WT: 1=on-site, 2=remote, 3=hybrid
    experience_levels: str | None = None  # f_E: comma-separated, e.g. "2,3"
