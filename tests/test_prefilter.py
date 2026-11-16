"""Prefilter tests use real job titles/companies captured from live LinkedIn
runs earlier in this project (see the seeded state.db) -- not synthetic
examples -- so the rules are validated against actual noise, not guesses.
"""

from pathlib import Path

import pytest

from jobradar.models import JobPosting
from jobradar.prefilter import prefilter
from jobradar.profile import load_profile

PROFILE = load_profile(Path(__file__).parent.parent / "profile.example.yaml")


def job(title: str, company: str = "Acme") -> JobPosting:
    return JobPosting(
        job_id="1",
        title=title,
        company=company,
        location="Remote",
        url="https://example.com",
        posted_label="1h ago",
        search_name="test",
    )


@pytest.mark.parametrize(
    "title,company",
    [
        ("GenAI Engineer", "Affinity"),
        ("Agentic AI engineer", "Xccelerated | Part of Xebia"),
        ("AI/ML Engineer (GenAI)", "Plain Concepts"),
    ],
)
def test_passes_clear_ai_roles(title, company):
    assert prefilter(job(title, company), PROFILE).passed is True


def test_rejects_academic_postdoc_at_a_university():
    result = prefilter(
        job(
            "Postdoc Predicting & Mitigating Liquid Copper Infiltration in Steels via Atomistic Simulations",
            "Delft University of Technology",
        ),
        PROFILE,
    )
    assert result.passed is False
    assert "dealbreaker" in result.reason


def test_rejects_phd_position_at_a_university():
    result = prefilter(
        job(
            "PhD Position eXtended Reality for Inclusive Automated Vehicle Interaction",
            "Delft University of Technology",
        ),
        PROFILE,
    )
    assert result.passed is False
    assert "dealbreaker" in result.reason


def test_rejects_head_of_role_regardless_of_ai_content():
    result = prefilter(job("Head of AI Factory", "AXA Partners"), PROFILE)
    assert result.passed is False
    assert "Head of" in result.reason


def test_rejects_consultant_title_with_no_ai_signal():
    result = prefilter(job("Senior Consultant - Financial Crime Prevention", "Zanders"), PROFILE)
    assert result.passed is False
    assert "no AI/ML signal" in result.reason


def test_ai_consultant_survives_the_exclusion_rule():
    # Same excluded word ("Consultant"), but the AI signal in the title saves it.
    result = prefilter(job("AI Consultant", "Some Co"), PROFILE)
    assert result.passed is True


def test_adjacent_roles_are_not_rejected_by_prefilter():
    # Data Engineer is "adjacent" -- relevance is a judgment call for the
    # LLM scorer, not something the deterministic filter can decide.
    result = prefilter(job("Data Engineer", "iXceed Solutions"), PROFILE)
    assert result.passed is True


@pytest.mark.parametrize(
    "title",
    ["Senior AI Engineer", "Staff Machine Learning Engineer", "Lead GenAI Engineer"],
)
def test_rejects_titles_above_the_seniority_ceiling(title):
    # PROFILE's seniority is entry-to-mid -- these are otherwise perfect
    # target-role matches, rejected purely on the seniority term.
    result = prefilter(job(title), PROFILE)
    assert result.passed is False
    assert "ceiling" in result.reason


def test_entry_to_mid_ai_engineer_survives_the_ceiling_rule():
    result = prefilter(job("AI Engineer"), PROFILE)
    assert result.passed is True
