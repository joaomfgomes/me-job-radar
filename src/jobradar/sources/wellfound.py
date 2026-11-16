"""Wellfound (ex-AngelList Talent) job source.

Wellfound's role/location pages are Next.js pages whose initial HTML embeds
a normalized Apollo GraphQL cache in <script id="__NEXT_DATA__">. The job
listings are already in there -- no client-side fetch, no headless browser,
no auth. We read that cache rather than scraping rendered markup, which is
both more stable and much richer than the visible cards.

Richer in two ways that matter to this pipeline:
  * `description` is the FULL job description, so a Wellfound posting needs
    no separate JD fetch -- the language check and deep scoring get their
    text in the same request that found the job.
  * `yearsExperienceMin` states the experience floor outright, so seniority
    can be filtered deterministically instead of inferred from prose.

Two quirks this handles explicitly:
  * Results include long-stale postings (liveStartAt years in the past), so
    the search window is enforced client-side -- there is no server-side
    recency filter to ask for.
  * Company names live in separate StartupResult entries; the job->company
    link is the reverse of StartupResult.highlightedJobListings.

Like the LinkedIn source, this is an undocumented internal shape and can
change without notice; _parse fails soft (skips malformed entries) so a
partial change degrades coverage instead of crashing the poll cycle.
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx
from bs4 import BeautifulSoup

from jobradar.models import JobPosting, SearchConfig
from jobradar.sources.linkedin import RateLimitedError

log = logging.getLogger(__name__)

BASE_URL = "https://wellfound.com/role/l"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_JOB_PREFIX = "JobListingSearchResult"
_STARTUP_PREFIX = "StartupResult"


def slugify(value: str) -> str:
    """'Machine Learning Engineer' -> 'machine-learning-engineer'."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


class WellfoundSource:
    name = "wellfound"

    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, search: SearchConfig) -> list[JobPosting]:
        url = f"{BASE_URL}/{slugify(search.keywords)}/{slugify(search.location)}"
        resp = self._client.get(url)

        if resp.status_code in (429, 503):
            raise RateLimitedError(f"Wellfound returned {resp.status_code} for search '{search.name}'")
        if resp.status_code == 404:
            # An unknown role/location slug is a config problem, not an
            # outage -- say so once and move on rather than tripping the
            # consecutive-error alarm.
            log.warning("Wellfound 404 for '%s' (%s) -- check the role/location slug", search.name, url)
            return []
        resp.raise_for_status()

        cutoff = time.time() - search.time_posted_seconds
        return self._parse(resp.text, search.name, cutoff)

    @staticmethod
    def _parse(html: str, search_name: str, cutoff: float) -> list[JobPosting]:
        tag = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
        if tag is None or not tag.string:
            log.warning("Wellfound: no __NEXT_DATA__ in response for '%s' -- markup may have changed", search_name)
            return []

        try:
            cache = json.loads(tag.string)["props"]["pageProps"]["apolloState"]["data"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            log.warning("Wellfound: unexpected __NEXT_DATA__ shape for '%s': %s", search_name, exc)
            return []

        company_by_job = WellfoundSource._company_by_job(cache)

        postings: list[JobPosting] = []
        for key, entry in cache.items():
            if not key.startswith(_JOB_PREFIX) or not isinstance(entry, dict):
                continue

            job_id = str(entry.get("id") or key.split(":", 1)[-1])
            live_start = entry.get("liveStartAt")
            # Wellfound happily returns postings years old; the caller's
            # window is the only thing keeping those out.
            if not isinstance(live_start, (int, float)) or live_start < cutoff:
                continue

            title = entry.get("title")
            if not title:
                continue

            locations = entry.get("locationNames") or []
            location = ", ".join(locations) if locations else ("Remote" if entry.get("remote") else "Unknown location")
            slug = entry.get("slug") or ""

            postings.append(
                JobPosting(
                    job_id=job_id,
                    title=title,
                    company=company_by_job.get(key, "Unknown company"),
                    location=location,
                    url=f"https://wellfound.com/jobs/{job_id}-{slug}".rstrip("-"),
                    posted_label=_relative_label(live_start),
                    search_name=search_name,
                    source="wellfound",
                    description=entry.get("description") or None,
                    years_experience_min=_as_int(entry.get("yearsExperienceMin")),
                )
            )
        return postings

    @staticmethod
    def _company_by_job(cache: dict) -> dict[str, str]:
        """StartupResult.highlightedJobListings points company -> jobs; the
        postings need the reverse."""
        mapping: dict[str, str] = {}
        for key, entry in cache.items():
            if not key.startswith(_STARTUP_PREFIX) or not isinstance(entry, dict):
                continue
            company = entry.get("name")
            if not company:
                continue
            for ref in entry.get("highlightedJobListings") or []:
                if isinstance(ref, dict) and "__ref" in ref:
                    mapping[ref["__ref"]] = company
        return mapping


def _as_int(value) -> int | None:
    return value if isinstance(value, int) else None


def _relative_label(live_start: float) -> str:
    """Mirrors the human 'x ago' string LinkedIn hands us directly."""
    seconds = max(0, time.time() - live_start)
    hours = int(seconds // 3600)
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"
