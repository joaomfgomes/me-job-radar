"""LinkedIn guest job-search source.

Uses the unauthenticated endpoint that LinkedIn's own logged-out job search
page calls (jobs-guest/jobs/api/seeMoreJobPostings/search). No login, no
cookies, no API key -- and therefore no account to ban. Returns an HTML
fragment (a list of <li> job cards), which we parse with BeautifulSoup.

This is not an official/documented API. It can change or disappear without
notice -- see JobSource for the fallback strategy.

Pagination: the endpoint returns at most PAGE_SIZE (10) cards per request,
regardless of how wide the f_TPR time window is -- verified against live
responses. A single request therefore silently truncates any search busier
than 10 postings per window, so fetch() walks `start` forward until a page
comes back partial (fewer than PAGE_SIZE cards), which is the endpoint's
own end-of-results signal (also verified: an out-of-range `start` returns
0 cards with HTTP 200, not an error). Deliberately no ordering assumption
-- the cards' <time datetime> values only carry day granularity, so
"stop once we've seen everything on this page" would not be sound.
"""

from __future__ import annotations

import logging
import re
import time

import httpx
from bs4 import BeautifulSoup

from jobradar.models import JobPosting, SearchConfig

log = logging.getLogger(__name__)

BASE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

# Hard cap the guest endpoint applies per request -- not a preference.
PAGE_SIZE = 10

# The guest endpoint rate-limited (HTTP 429) during real testing once a poll
# cycle started issuing pagination's back-to-back page requests on top of
# several searches in a row -- a burst pattern that didn't exist before
# pagination. 0.6s spacing still got 429s in practice; a live 12-request
# burst test at 1.2s spacing came back clean (12/12 status 200), so that's
# the empirically-set floor, not a guess. Tracked per source instance so it
# covers both within-search pagination and back-to-back searches sharing it.
MIN_REQUEST_INTERVAL_SECONDS = 1.2

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Raised when the response looks like a block/challenge rather than a normal
# empty result, so callers can distinguish "no new jobs" from "we got rate
# limited" and back off accordingly.
class RateLimitedError(RuntimeError):
    pass


class LinkedInGuestSource:
    def __init__(self, timeout: float = 15.0) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
        )
        self._last_request_at = 0.0

    def close(self) -> None:
        self._client.close()

    def fetch(self, search: SearchConfig) -> list[JobPosting]:
        """Walks pages until the endpoint signals end-of-results (a partial
        page) or max_pages is hit -- see module docstring. A quiet search
        costs exactly one request, same as before pagination existed."""
        postings: list[JobPosting] = []
        seen_ids: set[str] = set()

        for page in range(search.max_pages):
            page_postings = self._fetch_page(search, start=page * PAGE_SIZE)
            for posting in page_postings:
                # The endpoint can repeat a card across pages when new jobs
                # are posted mid-walk and shift the offsets -- dedupe here
                # so a single fetch() never returns the same job twice.
                if posting.job_id not in seen_ids:
                    seen_ids.add(posting.job_id)
                    postings.append(posting)
            if len(page_postings) < PAGE_SIZE:
                break
        else:
            log.info(
                "'%s': hit max_pages=%d with a full last page -- results may be truncated",
                search.name, search.max_pages,
            )

        return postings

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _fetch_page(self, search: SearchConfig, start: int) -> list[JobPosting]:
        self._throttle()
        params: dict[str, str] = {
            "keywords": search.keywords,
            "location": search.location,
            "f_TPR": f"r{search.time_posted_seconds}",
            "start": str(start),
        }
        if search.workplace_type:
            params["f_WT"] = search.workplace_type

        resp = self._client.get(BASE_URL, params=params)

        if resp.status_code in (429, 999):
            raise RateLimitedError(f"LinkedIn returned {resp.status_code} for search '{search.name}'")
        resp.raise_for_status()

        return self._parse(resp.text, search.name)

    @staticmethod
    def _parse(html: str, search_name: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "html.parser")
        postings: list[JobPosting] = []

        for card in soup.select("li > div.base-card[data-entity-urn]"):
            urn = card.get("data-entity-urn", "")
            match = re.search(r"jobPosting:(\d+)", urn)
            if not match:
                continue
            job_id = match.group(1)

            title_el = card.select_one("h3.base-search-card__title")
            company_el = card.select_one("h4.base-search-card__subtitle")
            location_el = card.select_one("span.job-search-card__location")
            link_el = card.select_one("a.base-card__full-link")
            time_el = card.select_one("time")

            title = title_el.get_text(strip=True) if title_el else "Unknown title"
            company = company_el.get_text(strip=True) if company_el else "Unknown company"
            location = location_el.get_text(strip=True) if location_el else "Unknown location"
            url = link_el["href"].split("?")[0] if link_el and link_el.get("href") else ""
            if not url.startswith("https://"):
                # Missing or unexpected href -- the id alone rebuilds the
                # canonical URL, so an alert never goes out without a link.
                url = f"https://www.linkedin.com/jobs/view/{job_id}"
            posted_label = time_el.get_text(strip=True) if time_el else ""

            postings.append(
                JobPosting(
                    job_id=job_id,
                    title=title,
                    company=company,
                    location=location,
                    url=url,
                    posted_label=posted_label,
                    search_name=search_name,
                )
            )

        return postings
