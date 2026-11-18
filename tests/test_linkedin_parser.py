"""Parser tests run entirely offline against a saved real response.

fixtures/search_response.html was captured live from LinkedIn's guest
endpoint. When LinkedIn changes their markup, this fixture won't update
itself -- if these tests suddenly fail against a *freshly re-captured*
fixture, that's the signal to fix the parser.
"""

from pathlib import Path

import pytest

from jobradar.models import JobPosting, SearchConfig
from jobradar.sources.base import RateLimitedError
from jobradar.sources.linkedin import LinkedInGuestSource

FIXTURE = Path(__file__).parent / "fixtures" / "search_response.html"


def load_fixture() -> str:
    return FIXTURE.read_text()


def test_parses_all_job_cards():
    html = load_fixture()
    postings = LinkedInGuestSource._parse(html, search_name="test-search")
    assert len(postings) == 10  # matches data-entity-urn count in the fixture


def test_extracts_expected_fields_for_first_job():
    html = load_fixture()
    postings = LinkedInGuestSource._parse(html, search_name="test-search")
    first = postings[0]

    assert first.job_id == "4429923023"
    assert first.title == "GenAI Engineer"
    assert first.company == "Affinity"
    assert first.location == "Portugal"
    assert first.search_name == "test-search"
    assert first.url.startswith("https://pt.linkedin.com/jobs/view/")
    assert "?" not in first.url  # tracking params stripped
    assert first.posted_label  # non-empty relative time string


def test_job_ids_are_unique():
    html = load_fixture()
    postings = LinkedInGuestSource._parse(html, search_name="test-search")
    ids = [p.job_id for p in postings]
    assert len(ids) == len(set(ids))


def test_empty_html_returns_no_postings():
    postings = LinkedInGuestSource._parse("<html><body>no jobs</body></html>", search_name="x")
    assert postings == []


# --- pagination (fetch() walks pages until a partial one) ---------------



class _FakeSourcePaging(LinkedInGuestSource):
    """Overrides _fetch_page instead of hitting the network -- the real
    per-request HTTP behavior is covered separately; this tests the paging
    loop's own logic (when to stop, how far to walk, dedup)."""

    def __init__(self, pages: list[list]):
        # deliberately skip LinkedInGuestSource.__init__ -- no real client needed
        self._pages = pages
        self.calls: list[int] = []

    def _fetch_page(self, search, start):
        self.calls.append(start)
        idx = start // 10
        return self._pages[idx] if idx < len(self._pages) else []


def _postings(n, offset=0):
    return [
        JobPosting(job_id=str(offset + i), title=f"Job {offset + i}", company="Acme",
                   location="Remote", url="https://x", posted_label="1h", search_name="s")
        for i in range(n)
    ]


def test_fetch_stops_at_the_first_partial_page():
    src = _FakeSourcePaging([_postings(10), _postings(4, offset=10)])
    result = src.fetch(SearchConfig(name="s", keywords="k", location="l"))
    assert src.calls == [0, 10]  # stopped after the partial page, no third request
    assert len(result) == 14


def test_fetch_stops_immediately_on_a_short_first_page():
    src = _FakeSourcePaging([_postings(3)])
    result = src.fetch(SearchConfig(name="s", keywords="k", location="l"))
    assert src.calls == [0]
    assert len(result) == 3


def test_fetch_respects_max_pages_even_with_full_pages_throughout():
    src = _FakeSourcePaging([_postings(10, offset=n * 10) for n in range(5)])
    result = src.fetch(SearchConfig(name="s", keywords="k", location="l", max_pages=2))
    assert src.calls == [0, 10]
    assert len(result) == 20  # never asked for page 3+, even though more exists


def test_fetch_dedupes_a_job_repeated_across_pages():
    """A job can shift pages mid-walk if new postings land between requests
    -- fetch() must not return it twice."""
    p0 = _postings(10)
    p1 = [p0[-1]] + _postings(9, offset=100)  # last job of page 0 repeats on page 1
    src = _FakeSourcePaging([p0, p1])
    result = src.fetch(SearchConfig(name="s", keywords="k", location="l"))
    assert len(result) == 19
    assert len({p.job_id for p in result}) == 19


# --- rate limiting ---------------------------------------------------------

class _Response:
    def __init__(self, status_code, text=""):
        self.status_code, self.text = status_code, text

    def raise_for_status(self):
        pass


def _source_answering(monkeypatch, statuses):
    """A real source whose HTTP client answers with the given status codes
    in order, and whose waits are instant."""
    source = LinkedInGuestSource()
    answers = iter(statuses)
    monkeypatch.setattr(source._client, "get", lambda *a, **kw: _Response(next(answers)))
    monkeypatch.setattr("jobradar.sources.linkedin.time.sleep", lambda seconds: None)
    return source


def test_a_rate_limited_request_is_retried_once(monkeypatch):
    source = _source_answering(monkeypatch, [429, 200])
    assert source.fetch(SearchConfig(name="s", keywords="k", location="l")) == []
    source.close()


def test_a_second_rate_limit_is_reported_not_swallowed(monkeypatch):
    source = _source_answering(monkeypatch, [429, 999])
    with pytest.raises(RateLimitedError):
        source.fetch(SearchConfig(name="s", keywords="k", location="l"))
    source.close()
