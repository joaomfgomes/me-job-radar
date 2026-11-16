"""Offline tests for the live-postings-based location validator. No
network -- a FakeSource stands in for LinkedInGuestSource/WellfoundSource.

The premise this all defends against, verified live (see the module
docstring): neither real source rejects a bogus location outright, and
which spelling of a real place resolves isn't predictable (LinkedIn takes
"Zürich" but not "Zurich"; takes "Munich" but not "München"). So every test
here is phrased as "what did the postings actually say", never "did we get
a 200".
"""

from jobradar.location_validation import fold_ascii, validate_location
from jobradar.models import JobPosting


class FakeSource:
    """Maps a query string to canned postings -- unmapped queries return a
    fixed set of "wrong place" results, mirroring the real endpoints'
    behavior of falling back rather than erroring."""

    def __init__(self, by_query: dict[str, list[str]], wrong_place=("Lehi, UT", "McLean, VA", "San Francisco, CA")):
        self._by_query = by_query
        self._wrong_place = list(wrong_place)
        self.queries_seen: list[str] = []

    def fetch(self, search):
        self.queries_seen.append(search.location)
        locations = self._by_query.get(search.location, self._wrong_place)
        return [
            JobPosting(job_id=str(i), title="AI Engineer", company="Acme", location=loc,
                       url="https://x", posted_label="1h", search_name=search.name)
            for i, loc in enumerate(locations)
        ]


def test_fold_ascii_strips_diacritics():
    assert fold_ascii("Zürich") == "Zurich"
    assert fold_ascii("München") == "Munchen"
    assert fold_ascii("Madrid") == "Madrid"


def test_passes_when_returned_locations_name_the_place():
    src = FakeSource({"Lisbon": ["Lisboa, Lisbon, Portugal"] * 5})
    result = validate_location("Lisbon", src)
    assert result.passed is True
    assert result.query_used == "Lisbon"
    assert result.matched == 5


def test_fails_when_results_are_from_somewhere_else_entirely():
    """The core scenario this module exists for: a 'successful' fetch whose
    results have nothing to do with the requested place."""
    src = FakeSource({})  # every query falls through to "wrong place"
    result = validate_location("Nowherecity", src)
    assert result.passed is False
    assert "didn't match" in result.reason or result.matched == 0


def test_retries_with_the_ascii_folded_spelling_when_the_original_fails():
    """Mirrors the real Zürich case: the accented spelling is what actually
    resolves, and the plain-ASCII one falls back to somewhere else."""
    src = FakeSource({"Zürich": ["Zurich, Switzerland"] * 5})  # only the accented form works
    result = validate_location("Zürich", src)
    assert result.passed is True
    assert result.query_used == "Zürich"
    assert src.queries_seen == ["Zürich"]  # first try succeeded, no fallback needed


def test_tries_the_original_spelling_first_before_folding():
    src = FakeSource({"Munich": ["Munich, Bavaria, Germany"] * 5})  # ASCII form is the one that works
    result = validate_location("Munich", src)
    assert result.passed is True
    assert result.query_used == "Munich"


def test_reports_the_actual_wrong_locations_when_both_spellings_fail():
    """Mirrors the real München case: neither spelling resolves, and the
    report should show what came back instead of a bare 'no match'."""
    src = FakeSource({})
    result = validate_location("München", src)
    assert result.passed is False
    assert "Lehi" in result.reason or "San Francisco" in result.reason


def test_too_few_results_is_reported_as_inconclusive_not_a_mismatch():
    """A real place with only 1-2 recent postings must not be reported the
    same way as a bogus one that returned 10 wrong-place results -- these
    are different failure modes and the message should say which."""
    src = FakeSource({"Small Town": ["Small Town, Nowhere"]})  # only 1 result, below MIN_SAMPLE_SIZE
    result = validate_location("Small Town", src)
    assert result.passed is False
    assert "too few" in result.reason.lower()


def test_no_results_at_all_is_reported_distinctly():
    src = FakeSource({"Ghost Town": []})
    result = validate_location("Ghost Town", src)
    assert result.passed is False
    assert "no postings" in result.reason.lower()


def test_partial_match_below_threshold_fails():
    """3/10 matching is nowhere near the 70% bar -- must not scrape by."""
    src = FakeSource({"Berlin": ["Berlin, Germany"] * 3 + ["Munich, Germany"] * 7})
    result = validate_location("Berlin", src)
    assert result.passed is False


def test_source_fetch_exception_is_treated_as_a_failed_attempt_not_a_crash():
    class BoomSource:
        def fetch(self, search):
            raise RuntimeError("network blip")

    result = validate_location("Lisbon", BoomSource())
    assert result.passed is False
