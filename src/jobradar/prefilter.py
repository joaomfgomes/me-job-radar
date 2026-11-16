"""Deterministic pre-filter -- hard rejects before any LLM call.

Pure and offline: given a JobPosting and a Profile, decide whether the
posting should be dropped outright (a dealbreaker) without spending an API
call on it. This is the "certainty on absolutes" layer -- an academic
posting or a "Head of X" role should never survive because a model had an
off day.

What passes still goes to the LLM scorer (scoring.py) for the judgment
calls a rule can't make -- e.g. whether a "Data Engineer" posting is
actually about LLM pipelines. Adjacent-role handling
(adjacent_roles_require_llm_tie) is deliberately NOT enforced here: this
filter only removes certainties, not maybes.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobradar.models import JobPosting
from jobradar.profile import Profile

AI_RELATED_HINTS = (
    "ai",
    "ml",
    "machine learning",
    "llm",
    "genai",
    "generative",
    "artificial intelligence",
    "nlp",
    "deep learning",
)


@dataclass(frozen=True)
class PrefilterResult:
    passed: bool
    reason: str | None = None


def _contains_any(haystack: str, needles: list[str] | tuple[str, ...]) -> str | None:
    """Case-insensitive substring search. Returns the matched needle, or None."""
    lowered = haystack.lower()
    for needle in needles:
        if needle.lower() in lowered:
            return needle
    return None


def prefilter(posting: JobPosting, profile: Profile) -> PrefilterResult:
    db = profile.dealbreakers

    match = _contains_any(posting.title, db.title_contains)
    if match:
        return PrefilterResult(False, f"title contains dealbreaker term '{match}'")

    match = _contains_any(posting.company, db.employer_contains)
    if match:
        return PrefilterResult(False, f"employer contains dealbreaker term '{match}'")

    excluded = _contains_any(posting.title, db.title_excludes_unless_ai_related)
    if excluded and not _contains_any(posting.title, AI_RELATED_HINTS):
        return PrefilterResult(False, f"title contains '{excluded}' with no AI/ML signal in the title")

    match = _contains_any(posting.title, db.title_contains_above_ceiling)
    if match:
        return PrefilterResult(
            False, f"title contains seniority term '{match}' above the candidate's ceiling ({profile.seniority})"
        )

    return PrefilterResult(True)
