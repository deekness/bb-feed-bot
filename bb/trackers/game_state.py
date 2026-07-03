"""Game-state tracker — the hard, authoritative facts (HOH / noms / veto / etc.).

Kept separate from the fuzzy social trackers. Each (week, role, houseguest)
fact is recorded with its confidence and supporting source. Week is derived
from the season start date.
"""
from __future__ import annotations

import logging
from datetime import date

from ..db import Database

log = logging.getLogger("bb.trackers.game_state")

_MIN_CONFIDENCE = 0.6  # ignore low-confidence game-state guesses


class GameStateTracker:
    def __init__(self, db: Database, season_start: date):
        self.db = db
        self.season_start = season_start

    def current_week(self, today: date | None = None) -> int:
        today = today or date.today()
        return max(1, ((today - self.season_start).days // 7) + 1)

    def current_day(self, today: date | None = None) -> int:
        today = today or date.today()
        return max(1, (today - self.season_start).days + 1)

    async def ingest(self, events: list) -> None:
        week = self.current_week()
        for e in events:
            if e.confidence < _MIN_CONFIDENCE:
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
