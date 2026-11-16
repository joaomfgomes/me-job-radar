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

    def close(self) -> None:
        """Release the source's HTTP client."""
        ...


class RateLimitedError(RuntimeError):
    """A board refused the request (HTTP 429, or LinkedIn's 999). Raised
    instead of returning an empty list so the pipeline can tell "no new
    jobs" apart from "we were blocked" and count it as a failure."""
