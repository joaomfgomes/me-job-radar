"""Loads profile.yaml -- the candidate profile fed to the pre-filter (prefilter.py)
and the LLM scorer (scoring.py). Kept separate from config.py: config.yaml/​.env are
pipeline plumbing, profile.yaml is career context the user owns and edits directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Dealbreakers:
    title_contains: list[str] = field(default_factory=list)
    employer_contains: list[str] = field(default_factory=list)
    title_excludes_unless_ai_related: list[str] = field(default_factory=list)
    # Terms that place a title at or above the candidate's seniority ceiling
    # (see Profile.seniority) -- a hard reject, since "Senior"/"Staff"/"Lead"
    # in a title is a certainty, not a judgment call.
    title_contains_above_ceiling: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Profile:
    target_roles: list[str]
    tech_stack: list[str]
    seniority: str
    # Numeric counterpart to `seniority`, for sources that state a required
    # experience floor outright (Wellfound does). None disables the check and
    # leaves seniority entirely to the prefilter and the scorer.
    max_years_experience: int | None
    languages: list[str]
    adjacent_roles_require_llm_tie: list[str]
    dealbreakers: Dealbreakers
    notes: str = ""
    # Free text describing where the candidate can work ("on-site only in
    # Porto; remote from Portugal"). Given to the deep pass, which answers
    # location_fit against it. Empty disables the location gate.
    location_preferences: str = ""


def load_profile(path: str | Path = "profile.yaml") -> Profile:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    db_raw = raw.get("dealbreakers") or {}
    dealbreakers = Dealbreakers(
        title_contains=db_raw.get("title_contains") or [],
        employer_contains=db_raw.get("employer_contains") or [],
        title_excludes_unless_ai_related=db_raw.get("title_excludes_unless_ai_related") or [],
        title_contains_above_ceiling=db_raw.get("title_contains_above_ceiling") or [],
    )

    return Profile(
        target_roles=raw.get("target_roles") or [],
        tech_stack=raw.get("tech_stack") or [],
        seniority=raw.get("seniority", "entry-to-mid"),
        max_years_experience=raw.get("max_years_experience"),
        languages=raw.get("languages") or [],
        adjacent_roles_require_llm_tie=raw.get("adjacent_roles_require_llm_tie") or [],
        dealbreakers=dealbreakers,
        notes=raw.get("notes") or "",
        location_preferences=raw.get("location_preferences") or "",
    )
