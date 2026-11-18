"""config.yaml -> Settings: how searches are generated, and what is
rejected at startup."""

import pytest
import yaml

from jobradar.config import build_searches, load_settings

LOCATIONS = [
    {"name": "Porto", "linkedin": "Porto, Portugal", "wellfound": "porto"},
    {"name": "Braga", "linkedin": "Braga, Portugal"},  # not covered on Wellfound
]
SOURCES = {
    "linkedin": {"keywords": ["AI Engineer", "LLM Engineer"], "time_posted_seconds": 1800, "max_pages": 5},
    "wellfound": {"keywords": ["ai-engineer"]},
}


def test_every_keyword_is_searched_in_every_location_the_board_covers():
    searches = build_searches(LOCATIONS, SOURCES)

    assert [s.name for s in searches] == [
        "AI Engineer - Porto (linkedin)", "AI Engineer - Braga (linkedin)",
        "LLM Engineer - Porto (linkedin)", "LLM Engineer - Braga (linkedin)",
        "ai-engineer - Porto (wellfound)",
    ]


def test_each_search_uses_its_boards_query_and_tuning():
    by_name = {s.name: s for s in build_searches(LOCATIONS, SOURCES)}

    linkedin = by_name["AI Engineer - Porto (linkedin)"]
    assert (linkedin.source, linkedin.location, linkedin.time_posted_seconds, linkedin.max_pages) == (
        "linkedin", "Porto, Portugal", 1800, 5)
    wellfound = by_name["ai-engineer - Porto (wellfound)"]
    assert (wellfound.source, wellfound.location, wellfound.time_posted_seconds) == ("wellfound", "porto", 3600)


def test_no_locations_or_no_keywords_yields_no_searches():
    assert build_searches([], SOURCES) == []
    assert build_searches(LOCATIONS, {"linkedin": None}) == []


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def write(**overrides):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"locations": LOCATIONS, "sources": SOURCES, **overrides}))
        return load_settings(path, env_path=tmp_path / "missing.env")

    return write


def test_defaults_apply_when_pipeline_is_omitted(write_config):
    settings = write_config()
    assert (settings.min_coverage_pct, settings.digest_min_coverage_pct, settings.years_stretch) == (60, 50, 1)
    assert settings.deep_model == "claude-haiku-5-5"
    assert len(settings.searches) == 5


def test_pipeline_values_override_defaults(write_config):
    assert write_config(pipeline={"min_coverage_pct": 70, "digest_min_coverage_pct": 50}).min_coverage_pct == 70


def test_out_of_range_values_are_all_reported_together(write_config):
    with pytest.raises(RuntimeError) as err:
        write_config(pipeline={"digest_hour": 25, "poll_interval_minutes": 0})
    assert "digest_hour" in str(err.value) and "poll_interval_minutes" in str(err.value)


def test_a_misspelt_setting_is_an_error_not_silently_ignored(write_config):
    with pytest.raises(RuntimeError, match="min_coverage"):
        write_config(pipeline={"min_coverage": 70})


def test_a_config_without_searches_is_rejected(write_config):
    with pytest.raises(RuntimeError, match="defines no searches"):
        write_config(locations=[])


def test_missing_telegram_credentials_are_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        load_settings(tmp_path / "config.yaml", env_path=tmp_path / "missing.env")
