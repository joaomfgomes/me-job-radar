"""Scoring tests run offline: ClaudeClient.parse is replaced by a stub, so
nothing here calls the API or needs ANTHROPIC_API_KEY. Building the client
with a dummy key makes no network call.
"""

import pytest

from jobradar.llm import ClaudeClient, LLMError
from jobradar.models import JobPosting
from jobradar.profile import Dealbreakers, Profile
from jobradar.scoring import (
    DeepScoreResult,
    JobScorer,
    Requirement,
    _DeepSchema,
    _TitleSchema,
    build_deep_prompt,
    build_title_prompt,
    candidate_context,
)


def make_profile(**overrides) -> Profile:
    fields = dict(
        target_roles=["AI Engineer", "LLM Engineer"],
        tech_stack=["Python", "RAG"],
        seniority="entry-to-mid",
        max_years_experience=3,
        languages=["English"],
        adjacent_roles_require_llm_tie=["Data Engineer"],
        dealbreakers=Dealbreakers(),
        notes="Prefer product companies over consultancies.",
        location_preferences="On-site only in Porto; remote from Portugal.",
    )
    return Profile(**{**fields, **overrides})


def make_posting(title: str = "AI Engineer", company: str = "Acme") -> JobPosting:
    return JobPosting(
        job_id="1", title=title, company=company, location="Remote",
        url="https://example.com", posted_label="1h ago", search_name="test",
    )


def deep_payload(**overrides) -> _DeepSchema:
    fields = dict(
        requirements=[
            {"requirement": "Python", "met": True, "evidence": "CV"},
            {"requirement": "Kubernetes", "met": False, "evidence": "not in CV"},
        ],
        years_required=None, is_junior_appropriate=True, location_fit=True,
        score=8, reason="Confirmed by full JD.",
        interview_chance_pct=55, chance_reason="Stack fits; no Kubernetes.",
        role_family=" AI Engineer ", seniority_level="junior", work_mode="remote",
        contract_type="Permanent", salary_min=40000, salary_max=55000, salary_currency="eur",
        skills=[
            {"name": "Python", "category": "language", "required": True, "candidate_has": True},
            {"name": "  ", "category": "other", "required": False, "candidate_has": False},
        ],
    )
    return _DeepSchema(**{**fields, **overrides})


@pytest.fixture
def scorer():
    scorer = JobScorer(ClaudeClient(api_key="test-key"), title_model="cheap-model", deep_model="strong-model")
    yield scorer
    scorer.close()


def stub(scorer, monkeypatch, result):
    """Make every parse() call return `result` (or raise it), recording the
    keyword arguments each call was made with."""
    calls = []

    def fake_parse(**kwargs):
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(scorer._client, "parse", fake_parse)
    return calls


# --- prompts --------------------------------------------------------------

def test_candidate_context_carries_profile_and_location_preferences():
    context = candidate_context(make_profile())
    assert "AI Engineer" in context
    assert "Data Engineer" in context  # adjacent-role note
    assert "Prefer product companies over consultancies." in context
    assert "remote from Portugal" in context
    assert "Years of professional experience: about 3 at most" in context


def test_candidate_context_includes_cv_when_given():
    assert "Built a RAG assistant" in candidate_context(make_profile(), "- Built a RAG assistant")
    assert "CV (extracted facts)" not in candidate_context(make_profile())


def test_candidate_context_is_deterministic():
    """It is the cached prompt prefix: two calls must be byte-identical."""
    assert candidate_context(make_profile(), "cv") == candidate_context(make_profile(), "cv")


def test_title_prompt_holds_only_the_card_fields():
    prompt = build_title_prompt(make_posting())
    assert "AI Engineer" in prompt and "Acme" in prompt and "Remote" in prompt


def test_deep_prompt_wraps_the_description_and_mentions_the_title_score():
    prompt = build_deep_prompt(make_posting(), "We need RAG experience.", title_score=6, title_score_reason="Plausible")
    assert "<job_description>\nWe need RAG experience.\n</job_description>" in prompt
    assert "6/10" in prompt and "Plausible" in prompt


def test_deep_prompt_works_without_a_title_score():
    assert "Preliminary" not in build_deep_prompt(make_posting(), "JD text.")


# --- title pass -----------------------------------------------------------

def test_score_parses_successful_response(scorer, monkeypatch):
    calls = stub(scorer, monkeypatch, _TitleSchema(score=9, reason="Strong match."))

    result = scorer.score(make_posting(), make_profile())

    assert (result.score, result.reason, result.unscored) == (9, "Strong match.", False)
    assert calls[0]["model"] == "cheap-model"
    # The candidate goes in the (cached) system prompt, the job in the user turn.
    assert "Target roles" in calls[0]["system"] and "Acme" in calls[0]["user"]


def test_score_clamps_out_of_range_values(scorer, monkeypatch):
    stub(scorer, monkeypatch, _TitleSchema(score=42, reason="?"))
    assert scorer.score(make_posting(), make_profile()).score == 10


@pytest.mark.parametrize("error", [ConnectionError("network down"), LLMError("stop_reason=refusal")])
def test_score_fails_open(scorer, monkeypatch, error):
    stub(scorer, monkeypatch, error)

    result = scorer.score(make_posting(), make_profile())

    assert result.unscored is True and result.score is None
    assert "scoring unavailable" in result.reason


# --- deep pass ------------------------------------------------------------

def test_score_deep_maps_the_schema_onto_the_result(scorer, monkeypatch):
    calls = stub(scorer, monkeypatch, deep_payload())

    result = scorer.score_deep(make_posting(), make_profile(), "JD text", cv_context="- RAG project")

    assert calls[0]["model"] == "strong-model"
    assert "RAG project" in calls[0]["system"]
    assert result.score == 8 and result.coverage_pct == 50
    assert [r.requirement for r in result.missing] == ["Kubernetes"]
    assert result.interview_chance_pct == 55
    assert result.location_fit is True
    insights = result.insights
    assert (insights.role_family, insights.contract_type, insights.salary_currency) == ("AI Engineer", "permanent", "EUR")
    assert [s.name for s in insights.skills] == ["Python"]  # the blank skill is dropped


def test_score_deep_plumbs_years_and_location_through(scorer, monkeypatch):
    stub(scorer, monkeypatch, deep_payload(years_required=5, location_fit=False, interview_chance_pct=140))

    result = scorer.score_deep(make_posting(), make_profile(), "JD text")

    assert result.years_required == 5
    assert result.location_fit is False
    assert result.interview_chance_pct == 100  # clamped


def test_score_deep_fails_open_on_error(scorer, monkeypatch):
    stub(scorer, monkeypatch, RuntimeError("boom"))

    result = scorer.score_deep(make_posting(), make_profile(), "JD text")

    assert result.unscored is True
    assert result.is_junior_appropriate is True and result.location_fit is True  # fail-open defaults


# --- coverage -------------------------------------------------------------

def test_coverage_pct_rounds_from_the_requirement_list():
    requirements = tuple(Requirement(f"r{i}", met=i < 2, evidence="") for i in range(3))
    assert DeepScoreResult(score=8, reason="", requirements=requirements).coverage_pct == 67


def test_coverage_pct_is_none_when_no_requirements_were_extracted():
    assert DeepScoreResult(score=8, reason="").coverage_pct is None


def test_the_prompt_leaves_stated_years_to_the_code():
    """The years gate is a soft limit applied in Python (evaluation.py). If
    the prompt also told the model to reject over-ceiling postings, every
    "4+ years" job would be caught as not junior-appropriate instead."""
    from jobradar.scoring import DEEP_INSTRUCTIONS

    assert "checked separately" in candidate_context(make_profile())
    assert "set it false only because of a stated number of years" in DEEP_INSTRUCTIONS
