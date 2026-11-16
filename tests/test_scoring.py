"""Scoring tests run offline: the OpenAI client's beta.chat.completions.parse
is stubbed, so these never call the real API and never need OPENAI_API_KEY
set. Constructing the OpenAI client itself makes no network call (lazy),
so instantiating OpenAIScorer with a dummy key is safe here.
"""

from jobradar.models import JobPosting
from jobradar.profile import Dealbreakers, Profile
from jobradar.scoring import OpenAIScorer, build_deep_prompt, build_prompt


def make_profile() -> Profile:
    return Profile(
        target_roles=["AI Engineer", "LLM Engineer"],
        tech_stack=["Python", "RAG"],
        seniority="entry-to-mid",
        max_years_experience=3,
        languages=["English"],
        adjacent_roles_require_llm_tie=["Data Engineer"],
        dealbreakers=Dealbreakers(),
        notes="Prefer product companies over consultancies.",
    )


def make_posting(title: str = "AI Engineer", company: str = "Acme") -> JobPosting:
    return JobPosting(
        job_id="1",
        title=title,
        company=company,
        location="Remote",
        url="https://example.com",
        posted_label="1h ago",
        search_name="test",
    )


def test_build_prompt_includes_profile_and_job_fields():
    prompt = build_prompt(make_posting(), make_profile())
    assert "AI Engineer" in prompt
    assert "Data Engineer" in prompt  # adjacent-tie note
    assert "Acme" in prompt
    assert "Prefer product companies over consultancies." in prompt


def test_build_prompt_omits_language_note():
    """The title-only pass has no description to judge language from --
    observed in production fabricating 'not in English or Spanish' claims
    about a title alone and dragging down otherwise-fair relevance scores
    (e.g. 'AI Compute Engineer' at Mistral, scored 2/10 partly on this).
    Language is already enforced deterministically on the real JD body
    before this pass ever runs -- see pipeline.py's cascade order."""
    prompt = build_prompt(make_posting(), make_profile())
    assert "score it very low" not in prompt


def test_build_deep_prompt_includes_language_note():
    """Unlike the title pass, the deep pass DOES have the real JD text, so
    the language instruction is legitimate here."""
    prompt = build_deep_prompt(make_posting(), make_profile(), jd_text="Some JD text.")
    assert "score it very low" in prompt


class _FakeParsed:
    def __init__(self, score: int, reason: str):
        self.score = score
        self.reason = reason


class _FakeMessage:
    def __init__(self, parsed):
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed, finish_reason: str = "stop"):
        self.message = _FakeMessage(parsed)
        self.finish_reason = finish_reason


class _FakeCompletion:
    def __init__(self, parsed, finish_reason: str = "stop"):
        self.choices = [_FakeChoice(parsed, finish_reason)]


def test_score_parses_successful_response(monkeypatch):
    scorer = OpenAIScorer(api_key="test-key")
    fake_completion = _FakeCompletion(_FakeParsed(9, "Strong match."))
    monkeypatch.setattr(scorer._client.beta.chat.completions, "parse", lambda **kw: fake_completion)

    result = scorer.score(make_posting(), make_profile())

    assert result.score == 9
    assert result.reason == "Strong match."
    assert result.unscored is False
    scorer.close()


def test_score_fails_open_on_network_error(monkeypatch):
    scorer = OpenAIScorer(api_key="test-key")

    def boom(**kwargs):
        raise ConnectionError("no network")

    monkeypatch.setattr(scorer._client.beta.chat.completions, "parse", boom)

    result = scorer.score(make_posting(), make_profile())

    assert result.score is None
    assert result.unscored is True
    assert "no network" in result.reason
    scorer.close()


def test_score_fails_open_on_api_error(monkeypatch):
    scorer = OpenAIScorer(api_key="test-key")

    def boom(**kwargs):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(scorer._client.beta.chat.completions, "parse", boom)

    result = scorer.score(make_posting(), make_profile())

    assert result.unscored is True
    scorer.close()


def test_score_fails_open_on_refusal(monkeypatch):
    """A None .parsed (e.g. the model refused, or hit a non-"stop" finish
    reason like content_filter) must fail open, not raise or crash."""
    scorer = OpenAIScorer(api_key="test-key")
    fake_completion = _FakeCompletion(None, finish_reason="content_filter")
    monkeypatch.setattr(scorer._client.beta.chat.completions, "parse", lambda **kw: fake_completion)

    result = scorer.score(make_posting(), make_profile())

    assert result.score is None
    assert result.unscored is True
    scorer.close()


# --- deep scoring (step 4: JD fetching + deep scoring) -------------------


def test_build_deep_prompt_includes_jd_text_and_title_score_context():
    prompt = build_deep_prompt(
        make_posting(),
        make_profile(),
        jd_text="We need someone fluent in LangGraph and RAG evaluation.",
        title_score=4,
        title_score_reason="Title alone doesn't confirm LLM focus.",
    )
    assert "LangGraph and RAG evaluation" in prompt
    assert "Preliminary title-only score: 4/10" in prompt
    assert "Title alone doesn't confirm LLM focus." in prompt
    assert "AI Engineer" in prompt  # profile context still present


def test_build_deep_prompt_includes_cv_context_when_given():
    prompt = build_deep_prompt(
        make_posting(),
        make_profile(),
        jd_text="Some JD text.",
        cv_context="- RAG Assistant with Evaluation Harness",
    )
    assert "RAG Assistant with Evaluation Harness" in prompt


def test_build_deep_prompt_works_without_optional_context():
    """title_score and cv_context are both optional -- the cascade may run
    deep scoring standalone without a preliminary pass or CV facts."""
    prompt = build_deep_prompt(make_posting(), make_profile(), jd_text="Some JD text.")
    assert "Some JD text." in prompt
    assert "Preliminary title-only score" not in prompt


class _FakeReq:
    def __init__(self, requirement, met):
        self.requirement, self.met, self.evidence = requirement, met, "because"


class _FakeDeepParsed:
    def __init__(self, score, reason, requirements, junior=True, years_required=None):
        self.score, self.reason = score, reason
        self.requirements = requirements
        self.years_required = years_required
        self.is_junior_appropriate = junior


def test_score_deep_parses_successful_response(monkeypatch):
    scorer = OpenAIScorer(api_key="test-key")
    parsed = _FakeDeepParsed(8, "Confirmed by full JD.",
                             [_FakeReq("Python", True), _FakeReq("Kubernetes", False)])
    monkeypatch.setattr(scorer._client.beta.chat.completions, "parse",
                        lambda **kw: _FakeCompletion(parsed))

    result = scorer.score_deep(make_posting(), make_profile(), jd_text="Full description here.")

    assert result.score == 8
    assert result.reason == "Confirmed by full JD."
    assert result.unscored is False
    assert result.coverage_pct == 50
    assert [r.requirement for r in result.matched] == ["Python"]
    assert [r.requirement for r in result.missing] == ["Kubernetes"]
    scorer.close()


def test_coverage_pct_rounds_from_the_requirement_list():
    from jobradar.scoring import DeepScoreResult, Requirement
    reqs = tuple(Requirement(f"r{i}", i < 2, "e") for i in range(3))
    assert DeepScoreResult(score=7, reason="", requirements=reqs).coverage_pct == 67


def test_coverage_pct_is_none_when_no_requirements_were_extracted():
    """An empty list must not read as 0% -- the caller has to be able to
    tell 'nothing extracted' apart from 'nothing matched'."""
    from jobradar.scoring import DeepScoreResult
    assert DeepScoreResult(score=7, reason="").coverage_pct is None


def test_deep_result_defaults_to_junior_appropriate():
    from jobradar.scoring import DeepScoreResult
    assert DeepScoreResult(score=None, reason="failed", unscored=True).is_junior_appropriate is True


def test_score_deep_plumbs_years_required_through(monkeypatch):
    scorer = OpenAIScorer(api_key="test-key")
    parsed = _FakeDeepParsed(8, "ok", [_FakeReq("Python", True)], years_required=5)
    monkeypatch.setattr(scorer._client.beta.chat.completions, "parse", lambda **kw: _FakeCompletion(parsed))

    result = scorer.score_deep(make_posting(), make_profile(), jd_text="Full description here.")

    assert result.years_required == 5
    scorer.close()


def test_build_prompt_states_the_numeric_years_ceiling():
    """The model needs the exact number, not just the 'entry-to-mid' label,
    to make an unambiguous is_junior_appropriate call."""
    prompt = build_prompt(make_posting(), make_profile())
    assert "Maximum years of experience" in prompt
    assert "3" in prompt  # make_profile()'s max_years_experience


def test_title_and_deep_passes_use_their_configured_models(monkeypatch):
    scorer = OpenAIScorer(api_key="test-key", model="cheap-model", deep_model="strong-model")
    seen = []

    def fake_parse(**kw):
        seen.append(kw["model"])
        return _FakeCompletion(_FakeDeepParsed(8, "ok", [_FakeReq("Python", True)]))

    monkeypatch.setattr(scorer._client.beta.chat.completions, "parse", fake_parse)
    scorer.score(make_posting(), make_profile())
    scorer.score_deep(make_posting(), make_profile(), jd_text="JD")

    assert seen == ["cheap-model", "strong-model"]
    scorer.close()


def test_score_deep_fails_open_on_error(monkeypatch):
    scorer = OpenAIScorer(api_key="test-key")

    def boom(**kwargs):
        raise ConnectionError("no network")

    monkeypatch.setattr(scorer._client.beta.chat.completions, "parse", boom)

    result = scorer.score_deep(make_posting(), make_profile(), jd_text="Full description here.")

    assert result.score is None
    assert result.unscored is True
    assert "no network" in result.reason
    scorer.close()
