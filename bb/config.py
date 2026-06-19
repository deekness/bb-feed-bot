"""Configuration: process secrets from the environment, season data from YAML.

Two objects:
  Settings  - operational config (tokens, db, model, schedule). From env only.
  Season    - everything that changes between seasons (roster, dates, sources).
              From an editable YAML file so a new season is a one-file change.

Neutrality note: nothing in here, or anywhere downstream, weights one
houseguest differently from another. The roster is just a flat list.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Season:
    number: int
    name: str
    start_date: date
    rss_url: str
    roster: list[str]
    nicknames: dict[str, str]
    bluesky_accounts: list[str]
    bb_keywords: list[str]

    @classmethod
    def load(cls, path: str | Path) -> "Season":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"Season config not found at '{p}'. Copy season.example.yaml to "
                f"'{p}' and fill in the roster."
            )
        data = yaml.safe_load(p.read_text()) or {}
        return cls(
            number=int(data["season_number"]),
            name=data.get("season_name", f"Big Brother {data['season_number']}"),
            start_date=date.fromisoformat(str(data["season_start_date"])),
            rss_url=data["rss_url"],
            roster=[str(n).strip() for n in (data.get("roster") or [])],
            nicknames={str(k).lower(): str(v) for k, v in (data.get("nicknames") or {}).items()},
            bluesky_accounts=[str(a).strip() for a in (data.get("bluesky_accounts") or [])],
            bb_keywords=[str(k).lower() for k in (data.get("bb_keywords") or [])],
        )


@dataclass(frozen=True)
class Settings:
    discord_token: str
    database_url: str
    anthropic_api_key: str
    llm_model: str
    update_channel_id: int | None
    owner_id: int | None
    timezone: str
    season_config_path: str
    llm_rpm: int
    llm_rph: int

    @classmethod
    def from_env(cls) -> "Settings":
        def required(key: str) -> str:
            val = os.getenv(key)
            if not val:
                raise RuntimeError(f"Missing required environment variable: {key}")
            return val

        def opt_int(key: str) -> int | None:
            val = os.getenv(key)
            return int(val) if val and val.strip() else None

        return cls(
            discord_token=required("DISCORD_TOKEN"),
            database_url=required("DATABASE_URL"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            llm_model=os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001"),
            update_channel_id=opt_int("UPDATE_CHANNEL_ID"),
            owner_id=opt_int("OWNER_ID"),
            timezone=os.getenv("TIMEZONE", "US/Pacific"),
            season_config_path=os.getenv("SEASON_CONFIG", "season.yaml"),
            llm_rpm=int(os.getenv("LLM_RPM", "10")),
            llm_rph=int(os.getenv("LLM_RPH", "100")),
        )
