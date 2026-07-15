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
                                            firmness, fallback_target, source_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (week, voter) DO UPDATE
                    SET target = EXCLUDED.target, confidence = EXCLUDED.confidence,
                        evidence = EXCLUDED.evidence, firmness = EXCLUDED.firmness,
                        fallback_target = EXCLUDED.fallback_target,
                        source_hash = EXCLUDED.source_hash, updated_at = now()
                    """,
                    week, p.voter, p.target, p.confidence, p.evidence[:500],
                    getattr(p, "firmness", "leaning"),
                    getattr(p, "fallback_target", "") or "",
                    getattr(p, "source_hash", ""),
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
            SELECT DISTINCT ON (voter) voter, target, firmness, fallback_target
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

    async def plans(self, week: int) -> list[dict]:
        """Latest ranked plan per voter (same filters as current())."""
        last_evict = await self.db.fetchval(
            "SELECT max(set_at) FROM game_state WHERE role = 'evicted'")
        rows = await self.db.fetch(
            """
            SELECT DISTINCT ON (voter) voter, target, firmness, fallback_target
            FROM vote_plans
            WHERE week BETWEEN $1 AND $2
              AND ($3::timestamptz IS NULL OR updated_at > $3)
            ORDER BY voter, updated_at DESC
            """,
            max(1, week - 1), week, last_evict,
        )
        evicted = {r["houseguest"] for r in await self.db.fetch(
            "SELECT houseguest FROM game_state WHERE role = 'evicted'")}
        return [dict(r) for r in rows if r["voter"] not in evicted]

    @staticmethod
    def scenario_board(plans: list[dict], pair: tuple[str, str],
                       saved: str) -> dict[str, list[tuple[str, str]]]:
        """One Block Buster outcome: `saved` escaped, `pair` face the vote.

        Each voter's vote is their highest-ranked preference that is actually on
        the block — target first, else fallback. The saved houseguest becomes a
        VOTER (that's the twist), with their own stated preference applied. A
        voter with no ranked preference in the pair is listed under '?'.
        """
        a, b = pair
        board: dict[str, list[tuple[str, str]]] = {a: [], b: [], "?": []}
        for p in plans:
            voter = p["voter"]
            if voter in pair:
                continue                    # on the block: doesn't vote
            first, second = p["target"], p.get("fallback_target") or ""
            if first in pair:
                board[first].append((voter, p["firmness"]))
            elif second in pair:
                # second choice inferred -> never firmer than 'leaning'
                f = "unsure" if p["firmness"] == "unsure" else "leaning"
                board[second].append((voter, f))
            else:
                board["?"].append((voter, p["firmness"]))
        return board

    async def remove(self, voter: str, week: int) -> bool:
        result = await self.db.execute(
            "DELETE FROM vote_plans WHERE week = $1 AND voter = $2", week, voter)
        try:
            return int(result.split()[-1]) > 0
        except (ValueError, IndexError, AttributeError):
            return False
