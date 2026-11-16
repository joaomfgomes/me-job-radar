"""Parser tests run entirely offline against a saved real response.

fixtures/wellfound_response.html was captured live from Wellfound's
role/location page. Its job listings are embedded as a Next.js
__NEXT_DATA__/Apollo cache rather than rendered markup -- see
sources/wellfound.py's module docstring for why that's what's parsed.
"""

from pathlib import Path

from jobradar.sources.wellfound import WellfoundSource, slugify

FIXTURE = Path(__file__).parent / "fixtures" / "wellfound_response.html"
# liveStartAt of the newest posting in the fixture (late Aug 2026).
FIXTURE_CAPTURED_AT = 1787690544


def load_fixture() -> str:
    return FIXTURE.read_text()


def test_slugify_matches_wellfounds_url_convention():
    assert slugify("Machine Learning Engineer") == "machine-learning-engineer"
    assert slugify("AI Engineer") == "ai-engineer"


def test_parses_all_live_job_cards_within_a_wide_cutoff():
    postings = WellfoundSource._parse(load_fixture(), "test-search", cutoff=0)
    assert len(postings) == 30  # matches the JobListingSearchResult count in the fixture


def test_cutoff_excludes_postings_older_than_the_window():
    all_jobs = WellfoundSource._parse(load_fixture(), "test-search", cutoff=0)
    # Anchored to the fixture's own capture time, not time.time(): the
    # fixture is a frozen snapshot, so "7 days before now" drifts past every
    # posting in it and the test starts failing a week after capture.
    recent = WellfoundSource._parse(load_fixture(), "test-search", cutoff=FIXTURE_CAPTURED_AT - 7 * 86400)
    assert 0 < len(recent) < len(all_jobs)


def test_extracts_expected_fields_for_a_known_job():
    postings = WellfoundSource._parse(load_fixture(), "test-search", cutoff=0)
    by_id = {p.job_id: p for p in postings}
    job = by_id["4574455"]

    assert job.title == "AI/agents engineer"
    assert job.company == "Nanzen"  # resolved via StartupResult.highlightedJobListings
    assert job.source == "wellfound"
    assert job.url == "https://wellfound.com/jobs/4574455-ai-agents-engineer"
    assert job.search_name == "test-search"
    assert job.years_experience_min == 2
    assert job.description and len(job.description) > 200  # full JD, not a snippet


def test_postings_without_a_resolvable_company_still_come_through():
    """A missing StartupResult link shouldn't drop the job -- 'Unknown
    company' beats silently losing a posting."""
    postings = WellfoundSource._parse(load_fixture(), "test-search", cutoff=0)
    assert all(p.company for p in postings)  # never blank


def test_parse_degrades_softly_on_unexpected_markup():
    assert WellfoundSource._parse("<html><body>no next data here</body></html>", "s", cutoff=0) == []


def test_parse_degrades_softly_on_malformed_next_data():
    html = '<script id="__NEXT_DATA__">{not valid json</script>'
    assert WellfoundSource._parse(html, "s", cutoff=0) == []
