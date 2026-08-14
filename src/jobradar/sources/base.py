"""Pluggable job-source interface.

If LinkedIn's guest endpoint ever gets gated, a fallback source (Adzuna,
JSearch, a company career-page RSS feed, ...) is a new class implementing
this interface, not a rewrite of the pipeline.
"""

from __future__ import annotations

from typing import Protocol

from jobradar.models import JobPosting, SearchConfig


class JobSource(Protocol):
    def fetch(self, search: SearchConfig) -> list[JobPosting]:
        """Return postings matching the given search, newest-window only."""
        ...
