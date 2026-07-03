"""Pairwise relationship tracker — an affinity graph.

Each pair of houseguests has an affinity score in [-1, 1] that events nudge.
This is more robust than trying to name every relationship: "alliances" emerge
as positive clusters and named groups are handled separately by AllianceTracker.
Both names are roster-validated upstream.

Two guards against feed noise (audit fixes):
  * same-kind cooldown — one blowup gets reported by RSS and several Bluesky
    accounts within minutes; a repeat of the SAME event kind for a pair inside
    REPEAT_COOLDOWN_H hours records nothing (a different kind still applies —
    fight-then-makeup is real signal, not a duplicate);
  * daily decay toward zero — a week-1 feud shouldn't read as 'rivals' in
    week 8 without fresh events. decay() is called from the daily loop and
    drops stale labels once |affinity| falls below the threshold. 'showmance'
    is event-driven, not affinity-driven, so it survives decay until a
    showmance_end lands.
"""
from __future__ import annotations

import logging

from ..db import Database

log = logging.getLogger("bb.trackers.relationships")

_DELTAS = {
    "showmance_start": 0.40,
    "showmance_end": -0.30,
    "allied": 0.20,
    "conflict": -0.25,
    "betrayal": -0.50,
}


class RelationshipTracker:
    REPEAT_COOLDOWN_H = 3    # same event kind for the same pair inside this window = duplicate report
    DECAY_PER_DAY = 0.03     # affinity pulled toward 0 per day without a fresh event

    def __init__(self, db: Database):
        self.db = db

    async def ingest(self, changes: list) -> None:
        for c in changes:
            if len(c.houseguests) != 2:
                continue
            try:
                await self._apply(c.houseguests[0], c.houseguests[1], c.kind)
            except Exception as e:
                log.error("relationship ingest failed: %s", e)

    async def _apply(self, a: str, b: str, kind: str) -> None:
        delta = _DELTAS.get(kind)
        if delta is None:
            return
        hg_a, hg_b = sorted([a, b])
        row = await self.db.fetchrow(
            "SELECT affinity, label, last_event, updated_at FROM relationships "
            "WHERE hg_a = $1 AND hg_b = $2", hg_a, hg_b
        )
        if row and row["last_event"] == kind:
            import datetime as _dt
            age = _dt.datetime.now(_dt.timezone.utc) - row["updated_at"]
            if age < _dt.timedelta(hours=self.REPEAT_COOLDOWN_H):
                return  # duplicate report of the same event — already counted
        current = float(row["affinity"]) if row else 0.0
        affinity = max(-1.0, min(1.0, current + delta))
        label = self._label(affinity, kind)
        await self.db.execute(
            """
            INSERT INTO relationships (hg_a, hg_b, affinity, label, last_event, updated_at)
            VALUES ($1, $2, $3, $4, $5, now())
            ON CONFLICT (hg_a, hg_b) DO UPDATE
            SET affinity = $3, label = $4, last_event = $5, updated_at = now()
            """,
            hg_a, hg_b, affinity, label, kind,
        )

    @staticmethod
    def _label(affinity: float, kind: str) -> str | None:
        if kind in ("showmance_start", "showmance_end"):
            return "showmance" if affinity > 0 else None
        if affinity >= 0.4:
            return "allies"
        if affinity <= -0.4:
            return "rivals"
        return None

    async def decay(self) -> None:
        """Pull stale affinities one day's step toward zero and drop labels
        that no longer clear the threshold. Called once daily; only rows with
        no event in the last day move, so repeated runs don't compound."""
        await self.db.execute(
            """
            UPDATE relationships
            SET affinity = CASE
                WHEN affinity > 0 THEN GREATEST(0.0, affinity - $1)
                WHEN affinity < 0 THEN LEAST(0.0, affinity + $1)
                ELSE 0.0 END
            WHERE updated_at < now() - interval '1 day' AND affinity <> 0
            """,
            self.DECAY_PER_DAY,
        )
        await self.db.execute(
            "UPDATE relationships SET label = NULL "
            "WHERE label IN ('allies', 'rivals') AND abs(affinity) < 0.4"
        )

    async def for_houseguest(self, name: str) -> list[dict]:
        rows = await self.db.fetch(
            """
            SELECT hg_a, hg_b, affinity, label FROM relationships
            WHERE hg_a = $1 OR hg_b = $1
            ORDER BY abs(affinity) DESC
            """,
            name,
        )
        out = []
        for r in rows:
            other = r["hg_b"] if r["hg_a"] == name else r["hg_a"]
            out.append({"other": other, "affinity": r["affinity"], "label": r["label"]})
        return out
