"""Offline tests for CV ingestion. The OpenAI call is stubbed (same pattern
as test_scoring.py) -- these never hit the network or need OPENAI_API_KEY.

The scrub_placeholders tests matter most: they're what verifies the
anti-fabrication guarantee actually holds before any text reaches the LLM.
"""

import yaml

from jobradar.cv_ingest import (
    CVIngestor,
    ExperienceEntry,
    ExtractedProfile,
    LanguageProficiency,
    format_cv_context,
    scrub_placeholders,
    write_generated_profile,
)


def test_scrub_replaces_portuguese_placeholder():
    text = "Spanish: [PREENCHER: nível de espanhol — relevante para Madrid]"
    assert scrub_placeholders(text) == "Spanish: [UNSPECIFIED]"


def test_scrub_replaces_multiple_placeholders_independently():
    text = (
        "Email: [PREENCHER: email] · Phone: [PREENCHER: telefone]\n"
        "Real content stays untouched."
    )
    result = scrub_placeholders(text)
    assert result.count("[UNSPECIFIED]") == 2
    assert "Real content stays untouched." in result
    assert "PREENCHER" not in result  # the instructional text itself must not survive


def test_scrub_handles_todo_and_fill_variants():
    assert scrub_placeholders("Bullet: [TODO: add metric]") == "Bullet: [UNSPECIFIED]"
    assert scrub_placeholders("Bullet: [FILL: add metric]") == "Bullet: [UNSPECIFIED]"


def test_scrub_is_case_insensitive():
    assert scrub_placeholders("[preencher: x]") == "[UNSPECIFIED]"


def test_scrub_leaves_real_bracketed_text_alone():
    # A real CV line like "AWS (Bedrock, Lambda)" or "[Open Source]" must survive.
    text = "AWS (Bedrock, Lambda) [Open Source]"
    assert scrub_placeholders(text) == text


class _FakeParsed:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


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


def _sample_extracted_profile() -> ExtractedProfile:
    return ExtractedProfile(
        tech_stack=["Python", "LangGraph", "MCP"],
        certifications=["Claude Certified Architect"],
        experience=[
            ExperienceEntry(role="AI Engineer", company="Acme", period="2025-Present", details="Built things."),
            ExperienceEntry(role="Researcher", company="Some Institute", period="2023-2024", details="unspecified"),
        ],
        education=["MSc, Management Information Systems"],
        languages=[
            LanguageProficiency(language="Portuguese", proficiency="Native"),
            LanguageProficiency(language="Spanish", proficiency="unspecified"),
        ],
        notable_projects=["RAG Assistant with Evaluation Harness"],
        seniority_signal="~2 years combined research + industry",
    )


def test_extract_parses_successful_response(monkeypatch):
    ingestor = CVIngestor(api_key="test-key")
    fake_profile = _sample_extracted_profile()
    monkeypatch.setattr(
        ingestor._client.beta.chat.completions, "parse", lambda **kw: _FakeCompletion(fake_profile)
    )

    result = ingestor.extract("scrubbed cv text")

    assert result.tech_stack == ["Python", "LangGraph", "MCP"]
    assert result.experience[1].details == "unspecified"
    ingestor.close()


def test_extract_raises_on_refusal(monkeypatch):
    """Unlike the scorer, CV ingestion is a manual one-shot run -- it should
    raise loudly on failure rather than fail open (there's no 'notify
    anyway' fallback for a CLI tool)."""
    ingestor = CVIngestor(api_key="test-key")
    monkeypatch.setattr(
        ingestor._client.beta.chat.completions,
        "parse",
        lambda **kw: _FakeCompletion(None, finish_reason="content_filter"),
    )

    try:
        ingestor.extract("scrubbed cv text")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "content_filter" in str(exc)
    ingestor.close()


def test_write_generated_profile_preserves_unspecified(tmp_path):
    profile = _sample_extracted_profile()
    out_path = tmp_path / "profile.generated.yaml"

    write_generated_profile(profile, out_path)

    content = out_path.read_text()
    assert "DO NOT hand-edit" in content  # header warns against manual edits

    data = yaml.safe_load(content)
    assert data["tech_stack"] == ["Python", "LangGraph", "MCP"]
    assert data["languages"]["Spanish"] == "unspecified"
    assert data["experience"][1]["details"] == "unspecified"
    assert data["experience"][0]["company"] == "Acme"


def test_format_cv_context_returns_none_when_file_missing(tmp_path):
    assert format_cv_context(tmp_path / "does_not_exist.yaml") is None


def test_format_cv_context_includes_real_experience_and_projects(tmp_path):
    profile = _sample_extracted_profile()
    out_path = tmp_path / "profile.generated.yaml"
    write_generated_profile(profile, out_path)

    context = format_cv_context(out_path)

    assert context is not None
    assert "AI Engineer at Acme: Built things." in context
    assert "RAG Assistant with Evaluation Harness" in context


def test_format_cv_context_skips_unspecified_entries(tmp_path):
    """The one field that matters most: a blank placeholder must never
    reach the deep-scoring prompt as if it were real content."""
    profile = _sample_extracted_profile()
    out_path = tmp_path / "profile.generated.yaml"
    write_generated_profile(profile, out_path)

    context = format_cv_context(out_path)

    assert context is not None
    assert "unspecified" not in context.lower()
    assert "Some Institute" not in context  # the unspecified-details role must not appear at all


def test_format_cv_context_returns_none_when_nothing_usable(tmp_path):
    out_path = tmp_path / "profile.generated.yaml"
    out_path.write_text("experience: []\nnotable_projects: []\n")

    assert format_cv_context(out_path) is None


def test_format_cv_context_handles_malformed_yaml(tmp_path):
    out_path = tmp_path / "profile.generated.yaml"
    out_path.write_text("not: valid: yaml: [")

    assert format_cv_context(out_path) is None
