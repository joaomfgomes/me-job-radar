"""Loads config.yaml (saved searches + pipeline settings) and .env (secrets)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from jobradar.llm import DEFAULT_MODEL
from jobradar.models import SearchConfig


@dataclass
class Settings:
    searches: list[SearchConfig]
    telegram_bot_token: str
    telegram_chat_id: str
    db_path: str = "state.db"
    poll_interval_minutes: int = 10
    poll_jitter_seconds: int = 60
    consecutive_error_alert_threshold: int = 3
    consecutive_empty_alert_threshold: int = 24  # ~4h at 10-min interval
    log_path: str = "jobradar.log"
    profile_path: str = "profile.yaml"  # filtering is off if this file doesn't exist
    # Searches worth watching but not worth polling
    # every cycle -- rotated through in small batches instead of all at once,
    # so coverage grows without multiplying LinkedIn traffic 1:1. See
    # Pipeline._next_secondary_batch().
    secondary_searches: list[SearchConfig] = field(default_factory=list)
    secondary_batch_size: int = 2
    # A job's title-only score must be >= this for its description to be
    # fetched and read by the deep pass. The empirical distribution from
    # the first week of observation was bimodal with a gap at 5 and 7 -- 4 is
    # the conservative choice: it catches every job that showed ANY
    # ambiguity on the title pass, without spending a JD fetch + LLM call
    # on the obvious-noise majority (scores 2-3, ~87% of volume).
    deep_score_threshold: int = 4
    # A job is only alerted if the deep pass finds at
    # least this share of the posting's must-have requirements covered by
    # the CV/profile, AND judges it junior-appropriate. Coverage is computed
    # in Python from the requirement list the model returns -- see
    # scoring.py -- not asked for as a number, so this threshold means
    # something concrete and auditable.
    min_coverage_pct: int = 80
    # Near-miss digest: jobs landing in [digest_min_coverage_pct,
    # min_coverage_pct) are held back from instant alerts but summarized
    # once a day, so an over-tight threshold is visible rather than silent.
    digest_min_coverage_pct: int = 60
    digest_hour: int = 9  # local-time hour to send the daily near-miss digest
    # Claude model ids for the two passes (see scoring.py). Separate
    # settings so the deep pass -- the harder task, and only run on the
    # minority of jobs that clear deep_score_threshold -- can be moved to a
    # stronger model (e.g. "claude-sonnet-5-5") without touching code.
    deep_model: str = DEFAULT_MODEL
    title_model: str = DEFAULT_MODEL
    # Raw matrix pieces (config.yaml's search_matrix.locations / .sources),
    # kept alongside the already-expanded `searches`/`secondary_searches` so
    # the pipeline can re-expand the matrix every cycle against whatever
    # locations exist right now -- including ones added at runtime via
    # /countries add, which config.yaml never sees. See Pipeline.current_searches().
    matrix_locations: list = field(default_factory=list)
    matrix_sources: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


def _parse_searches(raw_list: list[dict]) -> list[SearchConfig]:
    return [
        SearchConfig(
            name=s["name"],
            keywords=s["keywords"],
            location=s["location"],
            time_posted_seconds=s.get("time_posted_seconds", 1800),
            workplace_type=str(s["workplace_type"]) if s.get("workplace_type") else None,
            max_pages=s.get("max_pages", 3),
        )
        for s in raw_list
    ]


def _expand_matrix(matrix: dict) -> tuple[list[SearchConfig], list[SearchConfig]]:
    """Expands a per-source keywords x locations matrix into SearchConfigs.

    Adding a country is one entry under `locations` instead of one hand-written
    block per keyword per board. Returns (core, secondary): core keywords are
    polled every cycle, secondary ones rotate (Pipeline._next_secondary_batch).

    Keywords are nested under `sources` rather than shared, because boards
    don't agree on taxonomy or on posting velocity: LinkedIn takes free-text
    keywords and wants a ~30-minute window, while Wellfound takes URL slugs
    from its own role vocabulary and carries postings for weeks, so it needs
    a window measured in days to see anything at all. A location that omits a
    given source's key simply isn't searched on that board.

    Search names carry the source because seeded_searches is keyed by name --
    two boards running the same keyword in the same city would otherwise
    collide, and the second one would silently believe it had been seeded.
    """
    if not matrix:
        return [], []

    locations = matrix.get("locations") or []
    core: list[SearchConfig] = []
    secondary: list[SearchConfig] = []

    def build(source_name: str, keywords: list[str], tier_defaults: dict) -> list[SearchConfig]:
        out: list[SearchConfig] = []
        for keyword in keywords or []:
            for loc in locations:
                query = loc.get(source_name)
                if not query:
                    continue  # this location isn't covered on this board
                workplace_type = loc.get("workplace_type", tier_defaults.get("workplace_type"))
                out.append(
                    SearchConfig(
                        name=f"{keyword} - {loc['name']} ({source_name})",
                        keywords=keyword,
                        location=query,
                        time_posted_seconds=loc.get(
                            "time_posted_seconds", tier_defaults.get("time_posted_seconds", 1800)
                        ),
                        workplace_type=str(workplace_type) if workplace_type else None,
                        max_pages=loc.get("max_pages", tier_defaults.get("max_pages", 3)),
                        source=source_name,
                        location_name=loc["name"],
                    )
                )
        return out

    for source_name, spec in (matrix.get("sources") or {}).items():
        spec = spec or {}
        defaults = spec.get("defaults") or {}
        # secondary_defaults only needs to state what differs from defaults
        secondary_defaults = {**defaults, **(spec.get("secondary_defaults") or {})}
        core += build(source_name, spec.get("core_keywords"), defaults)
        secondary += build(source_name, spec.get("secondary_keywords"), secondary_defaults)

    return core, secondary


def expand_matrix_with_locations(locations: list[dict], sources: dict) -> tuple[list[SearchConfig], list[SearchConfig]]:
    """_expand_matrix() with the locations list swapped out -- used to
    re-expand against config.yaml's locations PLUS runtime-added ones
    without re-parsing config.yaml. See Settings.matrix_locations/matrix_sources."""
    return _expand_matrix({"locations": locations, "sources": sources})


def load_settings(config_path: str | Path = "config.yaml", env_path: str | Path = ".env") -> Settings:
    load_dotenv(env_path)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env "
            "(see .env.example)."
        )

    with open(config_path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise RuntimeError(f"{config_path} is empty or not a YAML mapping (see config.example.yaml).")

    # Matrix-expanded searches and hand-written ones coexist, but stay
    # separate here: `searches`/`secondary_searches` hold only the
    # hand-written ones. The matrix's raw pieces (matrix_locations/
    # matrix_sources below) are expanded fresh by the pipeline every cycle
    # instead of baked in once here, so /countries add takes effect without
    # a restart -- see Pipeline.current_searches().
    search_matrix = raw.get("search_matrix") or {}
    matrix_core, matrix_secondary = _expand_matrix(search_matrix)
    searches = _parse_searches(raw.get("searches", []))
    secondary_searches = _parse_searches(raw.get("secondary_searches", []))
    if not searches and not matrix_core:
        raise RuntimeError(f"No searches defined in {config_path}")

    pipeline = raw.get("pipeline") or {}

    settings = Settings(
        searches=searches,
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
        db_path=pipeline.get("db_path", "state.db"),
        poll_interval_minutes=pipeline.get("poll_interval_minutes", 10),
        poll_jitter_seconds=pipeline.get("poll_jitter_seconds", 60),
        consecutive_error_alert_threshold=pipeline.get("consecutive_error_alert_threshold", 3),
        consecutive_empty_alert_threshold=pipeline.get("consecutive_empty_alert_threshold", 24),
        log_path=pipeline.get("log_path", "jobradar.log"),
        profile_path=pipeline.get("profile_path", "profile.yaml"),
        secondary_searches=secondary_searches,
        secondary_batch_size=pipeline.get("secondary_batch_size", 2),
        deep_score_threshold=pipeline.get("deep_score_threshold", 4),
        min_coverage_pct=pipeline.get("min_coverage_pct", 80),
        digest_min_coverage_pct=pipeline.get("digest_min_coverage_pct", 60),
        digest_hour=pipeline.get("digest_hour", 9),
        deep_model=pipeline.get("deep_model", DEFAULT_MODEL),
        title_model=pipeline.get("title_model", DEFAULT_MODEL),
        matrix_locations=search_matrix.get("locations") or [],
        matrix_sources=search_matrix.get("sources") or {},
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    """Fail at startup with a readable message rather than hours later inside
    a scheduler thread (a non-integer interval, digest_hour: 25, ...)."""
    positive = ("poll_interval_minutes", "secondary_batch_size",
                "consecutive_error_alert_threshold", "consecutive_empty_alert_threshold")
    non_negative = ("poll_jitter_seconds", "deep_score_threshold")
    percentages = ("min_coverage_pct", "digest_min_coverage_pct")

    problems: list[str] = []
    for name in positive + non_negative + percentages + ("digest_hour",):
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"pipeline.{name} must be an integer (got {value!r})")
            continue
        if name in positive and value < 1:
            problems.append(f"pipeline.{name} must be >= 1 (got {value})")
        if name in non_negative and value < 0:
            problems.append(f"pipeline.{name} must be >= 0 (got {value})")
        if name in percentages and not 0 <= value <= 100:
            problems.append(f"pipeline.{name} must be between 0 and 100 (got {value})")
        if name == "digest_hour" and not 0 <= value <= 23:
            problems.append(f"pipeline.digest_hour must be between 0 and 23 (got {value})")
    if not problems and settings.digest_min_coverage_pct > settings.min_coverage_pct:
        problems.append("pipeline.digest_min_coverage_pct must not exceed pipeline.min_coverage_pct")

    for search in settings.searches + settings.secondary_searches:
        if search.time_posted_seconds < 1 or search.max_pages < 1:
            problems.append(f"search '{search.name}': time_posted_seconds and max_pages must be >= 1")

    if problems:
        raise RuntimeError("Invalid config.yaml:\n  - " + "\n  - ".join(problems))
