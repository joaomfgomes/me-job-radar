"""Offline tests for the keywords x locations search matrix (config.py's
_expand_matrix). No file I/O -- load_settings() itself is covered
indirectly since it's exercised end-to-end via config.yaml already.
"""

from jobradar.config import _expand_matrix


def matrix(**overrides):
    base = {
        "locations": [
            {"name": "Madrid", "linkedin": "Madrid, Spain", "wellfound": "madrid"},
            {"name": "Berlin", "linkedin": "Berlin, Germany"},  # no wellfound key
        ],
        "sources": {
            "linkedin": {
                "core_keywords": ["AI Engineer"],
                "secondary_keywords": ["LLM Engineer"],
                "defaults": {"time_posted_seconds": 1800},
                "secondary_defaults": {"time_posted_seconds": 7200},
            },
            "wellfound": {
                "secondary_keywords": ["ai-engineer"],
                "defaults": {"time_posted_seconds": 259200},
            },
        },
    }
    base.update(overrides)
    return base


def test_empty_matrix_yields_no_searches():
    assert _expand_matrix({}) == ([], [])


def test_core_and_secondary_split_by_source():
    core, secondary = _expand_matrix(matrix())
    assert {s.name for s in core} == {"AI Engineer - Madrid (linkedin)", "AI Engineer - Berlin (linkedin)"}
    assert {s.source for s in secondary} == {"linkedin", "wellfound"}


def test_a_location_missing_a_sources_key_is_skipped_on_that_board():
    core, secondary = _expand_matrix(matrix())
    wellfound_locations = {s.location_name for s in secondary if s.source == "wellfound"}
    assert wellfound_locations == {"Madrid"}  # Berlin has no wellfound slug


def test_location_name_is_preserved_for_the_countries_toggle():
    core, _ = _expand_matrix(matrix())
    assert all(s.location_name in ("Madrid", "Berlin") for s in core)


def test_secondary_defaults_falls_back_to_defaults_for_unset_keys():
    m = matrix()
    m["sources"]["linkedin"]["defaults"]["max_pages"] = 5
    del m["sources"]["linkedin"]["secondary_defaults"]["time_posted_seconds"]
    m["sources"]["linkedin"]["secondary_defaults"] = {}
    core, secondary = _expand_matrix(m)
    ln_secondary = [s for s in secondary if s.source == "linkedin"][0]
    assert ln_secondary.max_pages == 5  # inherited from defaults, not the hardcoded default of 3


def test_per_location_time_posted_seconds_overrides_tier_defaults():
    m = matrix()
    m["locations"][0]["time_posted_seconds"] = 999
    core, _ = _expand_matrix(m)
    madrid = next(s for s in core if s.location_name == "Madrid")
    assert madrid.time_posted_seconds == 999


def test_generated_names_are_unique_across_sources():
    core, secondary = _expand_matrix(matrix())
    names = [s.name for s in core + secondary]
    assert len(names) == len(set(names))


def _write_config(tmp_path, pipeline: dict):
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "searches": [{"name": "s", "keywords": "k", "location": "l"}],
        "pipeline": pipeline,
    }))
    return path


def test_load_settings_rejects_out_of_range_values(tmp_path, monkeypatch):
    import pytest

    from jobradar.config import load_settings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    path = _write_config(tmp_path, {"digest_hour": 25, "poll_interval_minutes": 0})
    with pytest.raises(RuntimeError) as err:
        load_settings(path, env_path=tmp_path / "missing.env")
    assert "digest_hour" in str(err.value) and "poll_interval_minutes" in str(err.value)


def test_load_settings_accepts_defaults(tmp_path, monkeypatch):
    from jobradar.config import load_settings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    settings = load_settings(_write_config(tmp_path, {}), env_path=tmp_path / "missing.env")
    assert settings.min_coverage_pct == 80
