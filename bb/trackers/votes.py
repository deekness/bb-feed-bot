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
                    INSERT INTO vote_plans (week, voter, target, confidence, evidence, source_hash)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (week, voter) DO UPDATE
                    SET target = EXCLUDED.target, confidence = EXCLUDED.confidence,
                        evidence = EXCLUDED.evidence, source_hash = EXCLUDED.source_hash,
                        updated_at = now()
                    """,
                    week, p.voter, p.target, p.confidence, p.evidence[:500],
                    getattr(p, "source_hash", ""),
                )
            except Exception as e:
                log.error("vote ingest failed: %s", e)

    async def current(self, week: int) -> dict[str, list[str]]:
        """target -> [voters], most recent plans only."""
        rows = await self.db.fetch(
            "SELECT voter, target FROM vote_plans WHERE week = $1 ORDER BY updated_at DESC",
            week,
        )
        counts: dict[str, list[str]] = {}
        for r in rows:
            counts.setdefault(r["target"], []).append(r["voter"])
        return counts

    async def remove(self, voter: str, week: int) -> bool:
        result = await self.db.execute(
            "DELETE FROM vote_plans WHERE week = $1 AND voter = $2", week, voter)
        try:
            return int(result.split()[-1]) > 0
        except (ValueError, IndexError, AttributeError):
            return False
