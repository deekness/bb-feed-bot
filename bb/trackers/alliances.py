"""Alliance tracker — evidence accumulation, not single-mention creation.

The LLM extractor proposes; this engine decides. An alliance becomes "active"
only after enough corroboration; confidence decays without fresh mentions;
and human confirm/reject (the `locked` flag) is never overwritten by the
automatic pipeline. Members are always roster-validated upstream.

Matching rule: a proposal merges into an existing alliance if they share
MERGE_OVERLAP+ members, otherwise a new (forming) alliance is created.
"""
from __future__ import annotations

import logging

from ..db import Database

log = logging.getLogger("bb.trackers.alliances")


class AllianceTracker:
    MERGE_OVERLAP = 2        # shared members required to treat as the same alliance
    CORROBORATION = 0.25     # confidence gained per fresh mention
    PROMOTE_AT = 0.6         # forming -> active threshold
    DECAY_PER_DAY = 0.08     # confidence lost per day without a mention
    DISSOLVE_BELOW = 0.15    # auto-dissolve threshold

    def __init__(self, db: Database):
        self.db = db

    async def ingest(self, proposals: list) -> None:
        for p in proposals:
            try:
                await self._ingest_one(p, getattr(p, "source_hash", ""))
            except Exception as e:
                log.error("alliance ingest failed: %s", e)

    async def _ingest_one(self, proposal, source_hash: str) -> None:
        match = await self._best_match(proposal.members)

        if match is None:
            await self._create(proposal)
            return

        alliance_id = match["id"]
        # Always record evidence + recency.
        await self.db.execute(
            "INSERT INTO alliance_evidence (alliance_id, quote, confidence, source_hash) "
            "VALUES ($1, $2, $3, $4)",
            alliance_id, proposal.evidence, proposal.confidence, source_hash,
        )
        await self.db.execute("UPDATE alliances SET last_seen = now() WHERE id = $1", alliance_id)

        if match["locked"]:
            # Human-decided (confirmed or rejected): do not auto-modify status/confidence.
            return

        new_conf = min(1.0, float(match["confidence"]) + self.CORROBORATION)
        status = match["status"]
        if status == "forming" and new_conf >= self.PROMOTE_AT:
            status = "active"
        if proposal.status in ("fracturing", "dissolved"):
            status = proposal.status
        # Adopt a name if we just learned one.
        name = match["name"] or proposal.name
        await self.db.execute(
            "UPDATE alliances SET confidence = $1, status = $2, name = $3 WHERE id = $4",
            new_conf, status, name, alliance_id,
        )
        # Merge any new members in.
        for hg in proposal.members:
            await self.db.execute(
                "INSERT INTO alliance_members (alliance_id, houseguest) VALUES ($1, $2) "
                "ON CONFLICT (alliance_id, houseguest) DO UPDATE SET active = TRUE",
                alliance_id, hg,
            )

    async def _best_match(self, members: list[str]):
        rows = await self.db.fetch(
            """
            SELECT a.id, a.name, a.status, a.confidence, a.locked,
                   array_agg(m.houseguest) AS members
            FROM alliances a
            JOIN alliance_members m ON m.alliance_id = a.id AND m.active
            GROUP BY a.id
            """
        )
        member_set = set(members)
        best, best_overlap = None, 0
        for r in rows:
            overlap = len(member_set & set(r["members"]))
            if overlap >= self.MERGE_OVERLAP and overlap > best_overlap:
                best, best_overlap = r, overlap
        return best

    async def _create(self, proposal) -> None:
        status = "active" if proposal.confidence >= self.PROMOTE_AT else "forming"
        row = await self.db.fetchrow(
            "INSERT INTO alliances (name, status, confidence) VALUES ($1, $2, $3) RETURNING id",
            proposal.name, status, proposal.confidence,
        )
        alliance_id = row["id"]
        for hg in proposal.members:
            await self.db.execute(
                "INSERT INTO alliance_members (alliance_id, houseguest) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                alliance_id, hg,
            )
        await self.db.execute(
            "INSERT INTO alliance_evidence (alliance_id, quote, confidence) VALUES ($1, $2, $3)",
            alliance_id, proposal.evidence, proposal.confidence,
        )
        log.info("new alliance #%s: %s (%s)", alliance_id,
                 proposal.name or "/".join(proposal.members), status)

    async def decay(self) -> int:
        """Lower confidence for stale (non-locked) alliances; dissolve the weakest."""
        await self.db.execute(
            """
            UPDATE alliances
            SET confidence = GREATEST(0, confidence -
                ($1 * EXTRACT(EPOCH FROM (now() - last_seen)) / 86400.0))
            WHERE NOT locked AND status <> 'dissolved'
            """,
            self.DECAY_PER_DAY,
        )
        result = await self.db.execute(
            "UPDATE alliances SET status = 'dissolved' "
            "WHERE NOT locked AND status <> 'dissolved' AND confidence < $1",
            self.DISSOLVE_BELOW,
        )
        return _rowcount(result)

    async def active(self) -> list[dict]:
        rows = await self.db.fetch(
            """
            SELECT a.id, a.name, a.status, a.confidence, a.locked,
                   array_agg(m.houseguest ORDER BY m.houseguest) AS members
            FROM alliances a
            JOIN alliance_members m ON m.alliance_id = a.id AND m.active
            WHERE a.status IN ('forming', 'active', 'fracturing')
            GROUP BY a.id
            ORDER BY a.confidence DESC, a.last_seen DESC
            """
        )
        return [dict(r) for r in rows]

    async def for_houseguest(self, name: str) -> list[dict]:
        rows = await self.db.fetch(
            """
            SELECT a.id, a.name, a.status, a.confidence, a.locked,
                   array_agg(m2.houseguest ORDER BY m2.houseguest) AS members
            FROM alliances a
            JOIN alliance_members m ON m.alliance_id = a.id AND m.active AND m.houseguest = $1
            JOIN alliance_members m2 ON m2.alliance_id = a.id AND m2.active
            WHERE a.status IN ('forming', 'active', 'fracturing')
            GROUP BY a.id
            ORDER BY a.confidence DESC
            """,
            name,
        )
        return [dict(r) for r in rows]

    async def confirm(self, alliance_id: int) -> bool:
        result = await self.db.execute(
            "UPDATE alliances SET locked = TRUE, status = 'active', confidence = 1.0 WHERE id = $1",
            alliance_id,
        )
        return _rowcount(result) > 0

    async def reject(self, alliance_id: int) -> bool:
        result = await self.db.execute(
            "UPDATE alliances SET locked = TRUE, status = 'dissolved', confidence = 0 WHERE id = $1",
            alliance_id,
        )
        return _rowcount(result) > 0


def _rowcount(status: str) -> int:
    # asyncpg returns e.g. "UPDATE 3"
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0
