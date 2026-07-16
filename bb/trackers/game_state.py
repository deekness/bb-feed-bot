"""Game-state tracker — the hard, authoritative facts (HOH / noms / veto / etc.).

Kept separate from the fuzzy social trackers. Each (week, role, houseguest)
fact is recorded with its confidence and supporting source. Week is derived
from the season start date.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from ..db import Database

log = logging.getLogger("bb.trackers.game_state")

_MIN_CONFIDENCE = 0.6  # ignore low-confidence game-state guesses

# Insertion order within a batch: prerequisites land before the roles that
# depend on them, so a single update reporting a full veto ceremony still works.
_ROLE_ORDER = {"hoh": 0, "have_not": 1, "nominee": 1, "veto_winner": 2, "veto_used_on": 3,
               "replacement_nominee": 4, "evicted": 5}

# Causal prerequisites. In Big Brother a replacement nominee CANNOT exist unless
# the veto was actually used — so a rumored/planned renom ("Melody is the renom
# if someone wins veto") is structurally rejected, no matter how confidently the
# feeds discuss it. This is the backstop for the LLM prompt rule.
_REQUIRES = {"replacement_nominee": "veto_used_on"}


class GameStateTracker:
    def __init__(self, db: Database, season_start: date,
                 house_day_one: date | None = None,
                 house_tz=None):
        self.db = db
        self.season_start = season_start        # premiere — drives WEEK math
        self.house_day_one = house_day_one      # move-in — drives DAY math
        self.house_tz = house_tz                # week/day math is HOUSE time

    def _today(self) -> date:
        """Today in house time. date.today() on the server is UTC, which rolls
        over at 7 PM Central — so week/day numbers flipped hours early every
        Wednesday night. That made the breaking stale-gate look at an empty
        'week 2', and episode-retell game events were WRITTEN into week 2,
        poisoning it before it began."""
        if self.house_tz is not None:
            return datetime.now(self.house_tz).date()
        return date.today()

    def current_week(self, today: date | None = None) -> int:
        today = today or self._today()
        return max(1, ((today - self.season_start).days // 7) + 1)

    def current_day(self, today: date | None = None) -> int:
        """The house day as the FEEDS count it. Big Brother's Day 1 is move-in
        day, which is several days before the premiere airs — so counting from
        the premiere made the bot's "Day 3" collide with the feeds' "Day 5".
        Falls back to the premiere date when house_day_one isn't configured."""
        today = today or self._today()
        day_one = self.house_day_one or self.season_start
        return max(1, (today - day_one).days + 1)

    async def _has_role(self, week: int, role: str) -> bool:
        return bool(await self.db.fetchval(
            "SELECT 1 FROM game_state WHERE week = $1 AND role = $2 LIMIT 1",
            week, role))

    async def ingest(self, events: list) -> None:
        week = self.current_week()
        # Prerequisites first, so a batch that reports the whole veto ceremony
        # (veto used AND the replacement) still records both.
        events = sorted(events, key=lambda e: _ROLE_ORDER.get(e.role, 99))
        for e in events:
            if e.confidence < _MIN_CONFIDENCE:
                continue
            prereq = _REQUIRES.get(e.role)
            if prereq and not await self._has_role(week, prereq):
                log.info("rejected %s=%s: no %s recorded this week (likely a "
                         "rumored plan, not a completed ceremony)",
                         e.role, e.houseguest, prereq)
                continue
            try:
                await self.db.execute(
                    """
                    INSERT INTO game_state (week, role, houseguest, confidence, source_hash)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (week, role, houseguest) DO UPDATE
                    SET confidence = GREATEST(game_state.confidence, EXCLUDED.confidence)
                    """,
                    week, e.role, e.houseguest, e.confidence,
                    getattr(e, "source_hash", ""),
                )
            except Exception as ex:
                log.error("game-state ingest failed: %s", ex)

    async def set_fact(self, role: str, houseguest: str,
                       week: int | None = None) -> None:
        """Admin override: record a fact at full confidence."""
        week = week or self.current_week()
        await self.db.execute(
            """
            INSERT INTO game_state (week, role, houseguest, confidence, source_hash)
            VALUES ($1, $2, $3, 1.0, 'admin')
            ON CONFLICT (week, role, houseguest) DO UPDATE
            SET confidence = 1.0, source_hash = 'admin', set_at = now()
            """,
            week, role, houseguest,
        )

    async def remove_fact(self, role: str, houseguest: str,
                          week: int | None = None) -> bool:
        """Admin override: delete a wrong fact. Returns True if a row was removed."""
        week = week or self.current_week()
        result = await self.db.execute(
            "DELETE FROM game_state WHERE week = $1 AND role = $2 AND houseguest = $3",
            week, role, houseguest,
        )
        try:
            return int(result.split()[-1]) > 0
        except (ValueError, IndexError, AttributeError):
            return False

    async def current(self, week: int | None = None) -> dict[str, list[str]]:
        week = week or self.current_week()
        rows = await self.db.fetch(
            "SELECT role, houseguest FROM game_state WHERE week = $1 ORDER BY role", week
        )
        state: dict[str, list[str]] = {}
        for r in rows:
            state.setdefault(r["role"], []).append(r["houseguest"])
        return state
