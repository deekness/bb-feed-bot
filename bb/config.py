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


_DAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
         "friday": 4, "saturday": 5, "sunday": 6}


def _parse_episodes(raw: list) -> list[dict]:
    """[{day, start 'HH:MM', end 'HH:MM', live}] -> [{weekday, start_min, end_min, live}]"""
    out = []
    for e in raw or []:
        try:
            day = _DAYS[str(e["day"]).strip().lower()]
            sh, sm = str(e["start"]).split(":")
            eh, em = str(e["end"]).split(":")
            out.append({"weekday": day,
                        "start_min": int(sh) * 60 + int(sm),
                        "end_min": int(eh) * 60 + int(em),
                        "live": bool(e.get("live", False))})
        except (KeyError, ValueError, TypeError):
            continue
    return out


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
    house_day_one: date | None = None
    rss_fallback_urls: list[str] = field(default_factory=list)
    rss_proxy_templates: list[str] = field(default_factory=list)
    episodes: list[dict] = field(default_factory=list)
    feedstate_enabled: bool = True
    feedstate_handle: str = "feed-bot.bsky.social"

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
            house_day_one=(date.fromisoformat(str(data["house_day_one"]))
                           if data.get("house_day_one") else None),
            rss_url=data["rss_url"],
            rss_fallback_urls=list(data.get("rss_fallback_urls") or []),
            rss_proxy_templates=list(data.get("rss_proxy_templates") or []),
            roster=[str(n).strip() for n in (data.get("roster") or [])],
            nicknames={str(k).lower(): str(v) for k, v in (data.get("nicknames") or {}).items()},
            bluesky_accounts=[str(a).strip() for a in (data.get("bluesky_accounts") or [])],
            bb_keywords=[str(k).lower() for k in (data.get("bb_keywords") or [])],
            episodes=_parse_episodes(data.get("episodes")),
            feedstate_enabled=bool((data.get("feed_state") or {}).get("enabled", True)),
            feedstate_handle=str((data.get("feed_state") or {}).get(
                "handle", "feed-bot.bsky.social")).strip(),
        )


@dataclass(frozen=True)
class Settings:
    discord_token: str
    database_url: str
    anthropic_api_key: str
    llm_model: str
    llm_model_recap: str
    update_channel_id: int | None
    recap_channel_id: int | None
    briefing_channel_id: int | None
    feeds_channel_id: int | None
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
            llm_model_recap=os.getenv("LLM_MODEL_RECAP", "").strip(),
            update_channel_id=opt_int("UPDATE_CHANNEL_ID"),
            recap_channel_id=opt_int("RECAP_CHANNEL_ID"),
            briefing_channel_id=opt_int("BRIEFING_CHANNEL_ID"),
            feeds_channel_id=opt_int("FEEDS_CHANNEL_ID"),
            owner_id=opt_int("OWNER_ID"),
            timezone=os.getenv("TIMEZONE", "US/Pacific"),
            season_config_path=os.getenv("SEASON_CONFIG", "season.yaml"),
            llm_rpm=int(os.getenv("LLM_RPM", "10")),
            llm_rph=int(os.getenv("LLM_RPH", "100")),
        )
