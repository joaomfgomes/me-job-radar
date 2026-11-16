"""Validates a location name against a job source before it's trusted.

Neither LinkedIn nor Wellfound reject an unrecognized location -- verified
live: LinkedIn's guest endpoint returns 10 cards from essentially random US
cities for a nonsense string, and Wellfound returns just as many results for
"asdfqwerzxcv" as for a real city slug. A naive "did we get results back?"
check would accept both as valid. Worse, the two sources don't even agree on
which spelling of a real place resolves correctly -- also verified live:
LinkedIn's "Zurich" silently falls back to Ontario, Canada, while "Zürich"
resolves correctly; for Munich it's the reverse ("Munich" works, "München"
falls back to Nuremberg). There is no single normalized form that's safe to
trust blindly.

So this checks the one thing that's actually reliable: what the returned
postings SAY their location is. A real match returns postings whose location
field overwhelmingly names the place asked for; a false match returns
postings from somewhere else entirely. Verified live at ~100% vs ~0% --
comfortably far apart for a threshold, not a coin flip.

Two spellings are tried (the name as given, and an ASCII-folded fallback)
before giving up, since which one resolves is source- and city-specific and
neither can be predicted -- see the Zurich/Munich cases above.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass

from jobradar.models import SearchConfig

log = logging.getLogger(__name__)

MIN_SAMPLE_SIZE = 3  # fewer results than this is inconclusive, not a pass
MATCH_THRESHOLD = 0.7  # share of sampled postings whose location must name the place


def fold_ascii(text: str) -> str:
    """'Zürich' -> 'Zurich', 'München' -> 'Munchen'. Used both as a retry
    spelling and to normalize before substring matching, so 'Zürich' can
    match a result that literally says 'Zurich, Switzerland'."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _normalize(text: str) -> str:
    return fold_ascii(text).lower().strip()


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    query_used: str  # the spelling that was actually sent, for the caller to persist
    sample_size: int
    matched: int
    reason: str


def _check_sample(name: str, locations: list[str]) -> tuple[bool, int, int]:
    needle = _normalize(name)
    matched = sum(1 for loc in locations if needle in _normalize(loc))
    if len(locations) < MIN_SAMPLE_SIZE:
        # Too few results to trust a ratio either way -- but the actual
        # count is still returned, so a caller can tell "2 results, both a
        # match, just not enough of them" from "2 results, neither matched".
        return False, len(locations), matched
    return (matched / len(locations)) >= MATCH_THRESHOLD, len(locations), matched


def validate_location(name: str, source, keywords: str = "AI Engineer", time_posted_seconds: int = 604800) -> ValidationResult:
    """`source` is anything with a .fetch(SearchConfig) -> list[JobPosting] --
    a LinkedInGuestSource, WellfoundSource, or a fake in tests. Fetches a
    sample search and checks the returned postings' own location field.
    """
    variants = [name]
    folded = fold_ascii(name)
    if folded != name:
        variants.append(folded)

    last_locations: list[str] = []
    for variant in variants:
        search = SearchConfig(name=f"validate:{variant}", keywords=keywords, location=variant,
                              time_posted_seconds=time_posted_seconds, max_pages=1)
        try:
            postings = source.fetch(search)
        except Exception as exc:
            log.warning("Location validation fetch failed for %r: %s", variant, exc)
            continue

        locations = [p.location for p in postings if p.location]
        last_locations = locations
        passed, sample_size, matched = _check_sample(name, locations)
        if passed:
            return ValidationResult(
                passed=True, query_used=variant, sample_size=sample_size, matched=matched,
                reason=f"{matched}/{sample_size} sampled postings named '{name}'",
            )

    # Nothing validated -- report what we actually got back, so a human can
    # tell "wrong city" from "too few results to be sure" from "no results
    # at all" instead of a single flat "didn't match".
    _, sample_size, matched = _check_sample(name, last_locations)
    if not last_locations:
        reason = "no postings returned for any spelling tried"
    elif sample_size < MIN_SAMPLE_SIZE:
        reason = f"only {sample_size} recent posting(s) to check ({matched} named '{name}') -- too few to be confident either way"
    else:
        examples = ", ".join(last_locations[:3])
        reason = f"{matched}/{sample_size} matched, below the {MATCH_THRESHOLD:.0%} bar -- e.g.: {examples}"
    return ValidationResult(passed=False, query_used=variants[-1], sample_size=sample_size, matched=matched, reason=reason)
