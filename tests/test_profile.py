from pathlib import Path

from jobradar.profile import load_profile

EXAMPLE_PROFILE = Path(__file__).parent.parent / "profile.example.yaml"


def test_loads_example_profile():
    profile = load_profile(EXAMPLE_PROFILE)
    assert "AI Engineer" in profile.target_roles
    assert "Python" in profile.tech_stack
    assert "Data Engineer" in profile.adjacent_roles_require_llm_tie
    assert "Postdoc" in profile.dealbreakers.title_contains
    assert "University" in profile.dealbreakers.employer_contains
    assert "Consultant" in profile.dealbreakers.title_excludes_unless_ai_related


def test_missing_sections_default_to_empty(tmp_path):
    minimal = tmp_path / "minimal.yaml"
    minimal.write_text("target_roles:\n  - AI Engineer\n")

    profile = load_profile(minimal)

    assert profile.target_roles == ["AI Engineer"]
    assert profile.dealbreakers.title_contains == []
    assert profile.adjacent_roles_require_llm_tie == []
    assert profile.notes == ""
