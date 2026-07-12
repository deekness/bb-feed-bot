"""Alliance tracker — evidence accumulation, not single-mention creation.

The LLM extractor proposes; this engine decides. An alliance becomes "active"
only after enough corroboration; confidence decays without fresh mentions;
Locking semantics: /confirmalliance pins confidence at 100% and exempts the
alliance from decay — but it can STILL be marked fracturing/dissolved by explicit
evidence, because confirmed alliances break up constantly in this game. A
/rejectalliance verdict is absolute: it stays dead and cannot be resurrected.
/unlockalliance returns an alliance to full automatic management.

Human confirm/reject (the `locked` flag) is never overwritten by the
automatic pipeline. Members are always roster-validated upstream.

Matching rules (audit fix — the old "any 2 shared members" rule snowballed
adjacent alliances into one blob and made final-2 deals impossible to track):
  * exact member-set match always merges (corroboration);
  * 2-person proposals (F2 deals / duos) ONLY merge on exact match — they are
    first-class entities in BB, never absorbed into a superset;
  * otherwise merge requires MERGE_OVERLAP+ shared members AND Jaccard
    similarity >= JACCARD_MIN, and two differently-NAMED alliances are always
    distinct (houseguests naming a group makes it its own entity);
  * an unlocked alliance that decayed to 'dissolved' is resurrected to
    'forming' by fresh evidence — but a human-rejected (locked) one stays dead
    and silently absorbs repeat proposals so it can't respawn.

Corroboration cooldown: the same conversation gets reported by RSS and several
Bluesky accounts within minutes, so confidence only bumps when the last
evidence is CORROBORATION_COOLDOWN_H+ hours old. Evidence rows and last_seen
are always recorded regardless.
"""
from __future__ import annotations

import logging

from ..db import Database

log = logging.getLogger("bb.trackers.alliances")


class AllianceTracker:
    MERGE_OVERLAP = 2        # shared members required to treat as the same alliance
    JACCARD_MIN = 0.55       # member-set similarity required to merge non-exact matches
    CORROBORATION = 0.25     # confidence gained per fresh (non-duplicate) mention
    CORROBORATION_COOLDOWN_H = 3  # min hours between confidence bumps
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
        match = await self._best_match(proposal.members, proposal.name)

        if match is None:
            await self._create(proposal)
            return

        alliance_id = match["id"]
        # Cooldown check BEFORE inserting the new evidence row.
        last_ev = await self.db.fetchval(
            "SELECT max(created_at) FROM alliance_evidence WHERE alliance_id = $1",
            alliance_id)
        # Always record evidence + recency.
        await self.db.execute(
            "INSERT INTO alliance_evidence (alliance_id, quote, confidence, source_hash) "
            "VALUES ($1, $2, $3, $4)",
            alliance_id, proposal.evidence, proposal.confidence, source_hash,
        )
        await self.db.execute("UPDATE alliances SET last_seen = now() WHERE id = $1", alliance_id)

        if match["locked"]:
            # Human-decided. A REJECTED alliance (locked + dissolved) stays dead —
            # fresh chatter must never resurrect something you ruled out.
            if match["status"] == "dissolved":
                return
            # A CONFIRMED alliance keeps its human-set confidence and is immune to
            # decay — but it can still BREAK UP. Confirmed alliances fracture all
            # the time in this game, and freezing them at 100% "active" forever
            # would make the bot blind to exactly the betrayal you most want to
            # know about. So explicit dissolution evidence still lands; only the
            # confidence drift is locked out.
            if proposal.status in ("fracturing", "dissolved"):
                await self.db.execute(
                    "UPDATE alliances SET status = $1 WHERE id = $2",
                    proposal.status, alliance_id)
                log.info("locked alliance #%s -> %s (explicit evidence)",
                         alliance_id, proposal.status)
            # Still adopt a name if they finally christen themselves.
            if proposal.name and not match["name"]:
                await self.db.execute(
                    "UPDATE alliances SET name = $1 WHERE id = $2",
                    proposal.name, alliance_id)
            return

        import datetime as _dt
        fresh = last_ev is None or (
            _dt.datetime.now(_dt.timezone.utc) - last_ev
        ) >= _dt.timedelta(hours=self.CORROBORATION_COOLDOWN_H)
        new_conf = (min(1.0, float(match["confidence"]) + self.CORROBORATION)
                    if fresh else float(match["confidence"]))
        status = match["status"]
        if status == "dissolved":
            status = "forming"   # resurrection: fresh evidence revives an auto-dissolved group
        if status == "forming" and new_conf >= self.PROMOTE_AT:
            status = "active"
        if proposal.status in ("fracturing", "dissolved"):
            status = proposal.status
        # Adopt a name if we just learned one.
        name = match["name"] or proposal.name
        one_sided = bool(match.get("one_sided")) or bool(getattr(proposal, "one_sided", False))
        await self.db.execute(
            "UPDATE alliances SET confidence = $1, status = $2, name = $3, "
            "one_sided = $4 WHERE id = $5",
            new_conf, status, name, one_sided, alliance_id,
        )
        # Merge any new members in.
        for hg in proposal.members:
            await self.db.execute(
                "INSERT INTO alliance_members (alliance_id, houseguest) VALUES ($1, $2) "
                "ON CONFLICT (alliance_id, houseguest) DO UPDATE SET active = TRUE",
                alliance_id, hg,
            )

    async def _best_match(self, members: list[str], name: str | None = None):
        rows = await self.db.fetch(
            """
            SELECT a.id, a.name, a.status, a.confidence, a.locked, a.one_sided,
                   array_agg(m.houseguest) AS members
            FROM alliances a
            JOIN alliance_members m ON m.alliance_id = a.id AND m.active
            GROUP BY a.id
            """
        )
        return self._pick_match(members, name, rows)

    def _pick_match(self, members: list[str], name: str | None, rows):
        """Pure matching logic (unit-testable). See module docstring for rules."""
        mset = set(members)
        best, best_j = None, 0.0
        for r in rows:
            rset = set(r["members"])
            if mset == rset:
                return r  # exact corroboration always wins
            if len(mset) == 2 or len(rset) == 2:
                continue  # duos are protected both ways: they never merge into
                          # supersets and never absorb them
            if name and r["name"] and name.strip().lower() != str(r["name"]).strip().lower():
                continue  # differently-named alliances are distinct entities
            overlap = len(mset & rset)
            j = overlap / len(mset | rset)
            if overlap >= self.MERGE_OVERLAP and j >= self.JACCARD_MIN and j > best_j:
                best, best_j = r, j
        return best

    async def _create(self, proposal) -> None:
        status = "active" if proposal.confidence >= self.PROMOTE_AT else "forming"
        row = await self.db.fetchrow(
            "INSERT INTO alliances (name, status, confidence, one_sided) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            proposal.name, status, proposal.confidence,
            bool(getattr(proposal, "one_sided", False)),
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
            SELECT a.id, a.name, a.status, a.confidence, a.locked, a.one_sided,
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
            SELECT a.id, a.name, a.status, a.confidence, a.locked, a.one_sided,
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

    async def detail(self, alliance_id: int) -> dict | None:
        row = await self.db.fetchrow(
            """
            SELECT a.id, a.name, a.status, a.confidence, a.locked, a.one_sided, a.first_seen,
                   a.last_seen, array_agg(m.houseguest ORDER BY m.houseguest) AS members
            FROM alliances a
            JOIN alliance_members m ON m.alliance_id = a.id AND m.active
            WHERE a.id = $1
            GROUP BY a.id
            """,
            alliance_id,
        )
        return dict(row) if row else None

    async def evidence(self, alliance_id: int, limit: int = 8) -> list[dict]:
        rows = await self.db.fetch(
            """
            SELECT e.quote, e.confidence, e.created_at, u.link
            FROM alliance_evidence e
            LEFT JOIN updates u ON u.content_hash = e.source_hash
            WHERE e.alliance_id = $1
            ORDER BY e.created_at DESC
            LIMIT $2
            """,
            alliance_id, limit,
        )
        return [dict(r) for r in rows]

    async def confirm(self, alliance_id: int) -> bool:
        result = await self.db.execute(
            "UPDATE alliances SET locked = TRUE, status = 'active', confidence = 1.0 WHERE id = $1",
            alliance_id,
        )
        return _rowcount(result) > 0

    async def unlock(self, alliance_id: int) -> bool:
        """Hand an alliance back to the tracker: confidence, promotion, decay and
        dissolution all resume automatically. Use when you'd rather the bot
        manage it than pin it by hand."""
        res = await self.db.execute(
            "UPDATE alliances SET locked = FALSE WHERE id = $1", alliance_id)
        return res.endswith("1")

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
