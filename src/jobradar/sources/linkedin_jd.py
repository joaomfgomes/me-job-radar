"""LinkedIn guest job-description fetcher.

Same unauthenticated-endpoint posture as linkedin.py (no login, no cookies,
no API key), a different guest endpoint: the full job-description page
rather than the search-results list. Used by the deep-scoring cascade
(pipeline.py) to give the LLM actual requirements instead of just a title.

Not an official/documented API, same caveat as linkedin.py -- it can
change or disappear without notice.
"""

from __future__ import annotations

import logging
import time

import httpx
from bs4 import BeautifulSoup

from jobradar.sources.linkedin import USER_AGENT

log = logging.getLogger(__name__)

BASE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting"

# Defensive cap against a pathological page, set well above real
# descriptions (observed: ~3.5k-6.2k chars) so that a long one is never cut
# off before its requirements section.
MAX_DESCRIPTION_CHARS = 20000

# A 429 here is a bigger deal than one on the search endpoint: a job whose
# JD we can't fetch skips the language check entirely and gets alerted
# unfiltered (see evaluation.py's fail-open cascade) -- exactly the failure
# mode that let a Dutch-only posting through once, right after a restart
# processed a burst of same-employer postings back-to-back. One retry
# after a short pause clears a transient burst-limit without adding
# meaningful latency to the (already post-restart-rare) unlucky case.
RETRY_DELAY_SECONDS = 3


class JobDescriptionFetcher:
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

    def fetch(self, job_id: str) -> str | None:
        """Full job description text, or None if unavailable.

        None covers network errors, non-2xx responses, and a missing
        description container alike -- callers treat all of these the
        same way: fall back to the title-only score, never crash the
        cascade over it. A 429/999 gets one retry first (see
        RETRY_DELAY_SECONDS) since it's usually a transient burst limit,
        not a real block.
        """
        for attempt in range(2):
            try:
                resp = self._client.get(f"{BASE_URL}/{job_id}")
            except httpx.HTTPError as exc:
                log.warning("JD fetch failed for job %s: %s", job_id, exc)
                return None

            if resp.status_code in (429, 999):
                if attempt == 0:
                    log.warning(
                        "JD fetch rate-limited for job %s -- retrying in %ss",
                        job_id, RETRY_DELAY_SECONDS,
                    )
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                log.warning("JD fetch still rate-limited for job %s after retry -- giving up", job_id)
                return None

            try:
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("JD fetch failed for job %s: %s", job_id, exc)
                return None

            return self._parse(resp.text)

        return None  # unreachable, satisfies type checkers

    @staticmethod
    def _parse(html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        desc = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
        if not desc:
            return None
        text = desc.get_text(separator="\n", strip=True)
        return text[:MAX_DESCRIPTION_CHARS] if text else None
