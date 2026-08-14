"""Parser tests run entirely offline against a saved real response.

fixtures/search_response.html was captured live from LinkedIn's guest
endpoint. When LinkedIn changes their markup, this fixture won't update
itself -- if these tests suddenly fail against a *freshly re-captured*
fixture, that's the signal to fix the parser.
"""

from pathlib import Path

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
