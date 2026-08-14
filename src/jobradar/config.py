"""Loads config.yaml (saved searches + pipeline settings) and .env (secrets)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

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
    extra: dict = field(default_factory=dict)


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

    searches = [
        SearchConfig(
            name=s["name"],
            keywords=s["keywords"],
            location=s["location"],
            time_posted_seconds=s.get("time_posted_seconds", 1800),
            workplace_type=str(s["workplace_type"]) if s.get("workplace_type") else None,
            experience_levels=str(s["experience_levels"]) if s.get("experience_levels") else None,
        )
        for s in raw.get("searches", [])
    ]
    if not searches:
        raise RuntimeError(f"No searches defined in {config_path}")

    pipeline = raw.get("pipeline", {})

    return Settings(
        searches=searches,
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
        db_path=pipeline.get("db_path", "state.db"),
        poll_interval_minutes=pipeline.get("poll_interval_minutes", 10),
        poll_jitter_seconds=pipeline.get("poll_jitter_seconds", 60),
        consecutive_error_alert_threshold=pipeline.get("consecutive_error_alert_threshold", 3),
        consecutive_empty_alert_threshold=pipeline.get("consecutive_empty_alert_threshold", 24),
        log_path=pipeline.get("log_path", "jobradar.log"),
    )
