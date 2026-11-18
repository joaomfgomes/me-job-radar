"""Loads config.yaml (searches and tuning) and .env (secrets) into Settings.

Searches are not written out one by one: config.yaml lists locations and,
per board, keywords, and every keyword is searched in every location that
board covers. Boards are configured separately because they disagree on
both vocabulary and tempo -- LinkedIn takes free-text keywords and turns
postings over within the hour, Wellfound takes URL slugs and keeps postings
up for weeks.

Everything is validated here, at startup, so a typo fails with a readable
message instead of hours later inside the scheduler thread.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

from jobradar.llm import DEFAULT_MODEL
from jobradar.models import SearchConfig
from jobradar.sources import SOURCES


@dataclass(frozen=True)
class Settings:
    searches: list[SearchConfig]
    telegram_bot_token: str
    telegram_chat_id: str
    db_path: str = "state.db"
    log_path: str = "jobradar.log"
    profile_path: str = "profile.yaml"  # filtering is off if this file doesn't exist
    poll_interval_minutes: int = 10
    poll_jitter_seconds: int = 60
    # Health alerts: consecutive failed fetches / empty results of one search
    # before a warning is sent. Empty results for hours usually mean a board
    # changed its markup and the parser silently broke.
    consecutive_error_alert_threshold: int = 3
    consecutive_empty_alert_threshold: int = 24
    # A job's title-only score must reach this for its description to be
    # fetched and read by the deep pass.
    deep_score_threshold: int = 4
    # A job is alerted only if at least this share of the posting's must-have
    # requirements is covered by the CV. Coverage is computed in Python from
    # the requirement list the model returns (see scoring.py).
    #
    # 60 rather than higher: hiring studies find candidates who meet about
    # half to 60% of a posting's requirements are interviewed about as often
    # as those who meet nearly all of them, and in this app's own data the
    # 60-79% band held as many good matches as the band above it.
    min_coverage_pct: int = 60
    # Near misses in [digest_min_coverage_pct, min_coverage_pct) are held
    # back but summarised once a day, so an over-tight bar is visible.
    digest_min_coverage_pct: int = 50
    # How many years above profile.yaml's max_years_experience a posting may
    # ask for and still be alerted (with a warning). See evaluation.py.
    years_stretch: int = 1
    digest_hour: int = 9
    # Claude model ids for the two passes (see scoring.py). Separate so the
    # deep pass can be moved to a stronger model without touching code.
    title_model: str = DEFAULT_MODEL
    deep_model: str = DEFAULT_MODEL


def build_searches(locations: list[dict], sources: dict[str, dict]) -> list[SearchConfig]:
    """Every keyword of every board, in every location that board covers.

    A location names its query per board (`linkedin: "Porto, Portugal"`,
    `wellfound: "porto"`); one that omits a board's key is not searched
    there. The search name carries the board because seeding is tracked by
    name -- the same keyword on two boards must not look like one search.
    """
    searches: list[SearchConfig] = []
    for source, spec in sources.items():
        spec = spec or {}
        for keyword in spec.get("keywords") or []:
            for location in locations:
                query = location.get(source)
                if not query:
                    continue
                searches.append(SearchConfig(
                    name=f"{keyword} - {location['name']} ({source})",
                    keywords=keyword,
                    location=query,
                    source=source,
                    time_posted_seconds=spec.get("time_posted_seconds", 3600),
                    max_pages=spec.get("max_pages", 3),
                ))
    return searches


def load_settings(config_path: str | Path = "config.yaml", env_path: str | Path = ".env") -> Settings:
    load_dotenv(env_path)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env (see .env.example).")

    with open(config_path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise RuntimeError(f"{config_path} is empty or not a YAML mapping (see config.example.yaml).")

    sources = raw.get("sources") or {}
    unknown_boards = set(sources) - set(SOURCES)
    if unknown_boards:
        raise RuntimeError(f"Unknown source(s) in {config_path}: {', '.join(sorted(unknown_boards))}. "
                           f"Known: {', '.join(sorted(SOURCES))}.")
    searches = build_searches(raw.get("locations") or [], sources)
    if not searches:
        raise RuntimeError(f"{config_path} defines no searches: it needs `locations` and `sources` with keywords.")

    # Only keys that are present override the dataclass defaults.
    tuning = raw.get("pipeline") or {}
    unknown = set(tuning) - set(Settings.__dataclass_fields__)
    if unknown:
        raise RuntimeError(f"Unknown pipeline setting(s) in {config_path}: {', '.join(sorted(unknown))}")

    settings = Settings(searches=searches, telegram_bot_token=bot_token, telegram_chat_id=chat_id, **tuning)
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    at_least_one = ("poll_interval_minutes", "consecutive_error_alert_threshold", "consecutive_empty_alert_threshold")
    non_negative = ("poll_jitter_seconds", "deep_score_threshold", "years_stretch")
    percentages = ("min_coverage_pct", "digest_min_coverage_pct")

    problems: list[str] = []
    for name in at_least_one + non_negative + percentages + ("digest_hour",):
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"pipeline.{name} must be an integer (got {value!r})")
        elif name in at_least_one and value < 1:
            problems.append(f"pipeline.{name} must be >= 1 (got {value})")
        elif name in non_negative and value < 0:
            problems.append(f"pipeline.{name} must be >= 0 (got {value})")
        elif name in percentages and not 0 <= value <= 100:
            problems.append(f"pipeline.{name} must be between 0 and 100 (got {value})")
        elif name == "digest_hour" and not 0 <= value <= 23:
            problems.append(f"pipeline.digest_hour must be between 0 and 23 (got {value})")
    if not problems and settings.digest_min_coverage_pct > settings.min_coverage_pct:
        problems.append("pipeline.digest_min_coverage_pct must not exceed pipeline.min_coverage_pct")

    for search in settings.searches:
        if search.time_posted_seconds < 1 or search.max_pages < 1:
            problems.append(f"search '{search.name}': time_posted_seconds and max_pages must be >= 1")

    if problems:
        raise RuntimeError("Invalid config.yaml:\n  - " + "\n  - ".join(problems))
