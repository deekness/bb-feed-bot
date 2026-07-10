"""Vote tracker — who plans to vote out whom this week.

Latest-plan-wins per voter per week: houseguests flip constantly, so each new
evidenced plan overwrites the voter's previous one. Names are roster-validated
upstream; a plan below the confidence floor is ignored.
"""
from __future__ import annotations

import logging

from ..db import Database

log = logging.getLogger("bb.trackers.votes")

_MIN_CONFIDENCE = 0.5


class VoteTracker:
    def __init__(self, db: Database):
        self.db = db

    async def ingest(self, plans: list, week: int) -> None:
        for p in plans:
            if p.confidence < _MIN_CONFIDENCE or p.voter == p.target:
                continue
            try:
                await self.db.execute(
                    """
                    INSERT INTO vote_plans (week, voter, target, confidence, evidence,
                                            firmness, source_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (week, voter) DO UPDATE
                    SET target = EXCLUDED.target, confidence = EXCLUDED.confidence,
                        evidence = EXCLUDED.evidence, firmness = EXCLUDED.firmness,
                        source_hash = EXCLUDED.source_hash, updated_at = now()
                    """,
                    week, p.voter, p.target, p.confidence, p.evidence[:500],
                    getattr(p, "firmness", "leaning"), getattr(p, "source_hash", ""),
                )
            except Exception as e:
                log.error("vote ingest failed: %s", e)

    async def current(self, week: int) -> dict[str, list[str]]:
        """target -> [voters], latest plan per voter.

        BB weeks straddle the calendar-week flip: plans stated Mon-Wed live in
        week N while eviction-day plans land in week N+1, so the current and
        previous week are merged. Two filters keep the board honest:
          * plans older than the most recent recorded eviction are dropped —
            the vote board resets when someone walks out the door, and
          * anyone already evicted is excluded as voter or target.
        """
        last_evict = await self.db.fetchval(
            "SELECT max(set_at) FROM game_state WHERE role = 'evicted'")
        rows = await self.db.fetch(
            """
            SELECT DISTINCT ON (voter) voter, target, firmness
            FROM vote_plans
            WHERE week BETWEEN $1 AND $2
              AND ($3::timestamptz IS NULL OR updated_at > $3)
            ORDER BY voter, updated_at DESC
            """,
            max(1, week - 1), week, last_evict,
        )
        evicted = {r["houseguest"] for r in await self.db.fetch(
            "SELECT houseguest FROM game_state WHERE role = 'evicted'")}
        counts: dict[str, list[tuple[str, str]]] = {}
        for r in rows:
            if r["voter"] in evicted or r["target"] in evicted:
                continue
            counts.setdefault(r["target"], []).append((r["voter"], r["firmness"]))
        return counts

    async def remove(self, voter: str, week: int) -> bool:
        result = await self.db.execute(
            "DELETE FROM vote_plans WHERE week = $1 AND voter = $2", week, voter)
        try:
            return int(result.split()[-1]) > 0
        except (ValueError, IndexError, AttributeError):
            return False
