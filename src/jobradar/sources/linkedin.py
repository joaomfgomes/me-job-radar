"""LinkedIn guest job-search source.

Uses the unauthenticated endpoint that LinkedIn's own logged-out job search
page calls (jobs-guest/jobs/api/seeMoreJobPostings/search). No login, no
cookies, no API key -- and therefore no account to ban. Returns an HTML
fragment (a list of <li> job cards), which we parse with BeautifulSoup.

This is not an official/documented API. It can change or disappear without
notice -- see JobSource for the fallback strategy.
"""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from jobradar.models import JobPosting, SearchConfig

log = logging.getLogger(__name__)

BASE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

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

    def close(self) -> None:
        self._client.close()

    def fetch(self, search: SearchConfig) -> list[JobPosting]:
        params: dict[str, str] = {
            "keywords": search.keywords,
            "location": search.location,
            "f_TPR": f"r{search.time_posted_seconds}",
            "start": "0",
        }
        if search.workplace_type:
            params["f_WT"] = search.workplace_type
        if search.experience_levels:
            params["f_E"] = search.experience_levels

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
