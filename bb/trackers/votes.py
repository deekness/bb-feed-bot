"""Vote tracker — who plans to vote out whom this week.

Houseguests flip constantly, so newer plans generally overwrite older ones —
but they also BLUFF, and the axis deception lives on is the AUDIENCE. So each
statement gets a credibility score from the alliance trust map:

  * said inside the voter's own trusted alliance      -> high credibility
  * audience unknown (updaters often omit it)         -> neutral
  * said TO the target, or to the target's allies     -> low (likely theatre)

and a low-credibility statement is NOT allowed to overwrite a clearly more
credible one — Haley telling Taylor's ally "I'm still deciding" must not erase
Haley telling her own alliance "I'm voting Taylor". A stale plan (>36h) can
always be overwritten: real flips happen, and old intel decays.
"""
from __future__ import annotations

import logging

from ..db import Database

log = logging.getLogger("bb.trackers.votes")

_MIN_CONFIDENCE = 0.5
# A new plan may replace an older DIFFERENT one only if it isn't clearly less
# credible; below this margin the old statement stands.
_CRED_MARGIN = 0.2
# ...unless the old plan is stale — houseguests genuinely flip.
_STALE_HOURS = 36
# Displayed as shaky below this, whatever the stated firmness.
LOW_CRED = 0.45


class VoteTracker:
    def __init__(self, db: Database):
        self.db = db

    async def _pair_strength(self, a: str, b: str) -> float:
        """How much `a` and `b` trust each other: the confidence of the
        strongest live alliance containing BOTH. 0.0 if none."""
        if not a or not b or a == b:
            return 0.0
        val = await self.db.fetchval(
            """
            SELECT max(al.confidence)
            FROM alliances al
            JOIN alliance_members m1 ON m1.alliance_id = al.id AND m1.active
                                     AND m1.houseguest = $1
            JOIN alliance_members m2 ON m2.alliance_id = al.id AND m2.active
                                     AND m2.houseguest = $2
            WHERE al.status IN ('forming', 'active')
            """,
            a, b,
        )
        return float(val or 0.0)

    async def credibility(self, voter: str, target: str,
                          said_to: list[str]) -> float:
        """How much to believe a stated vote, given who heard it."""
        if not said_to:
            return 0.6                          # audience unknown: neutral
        if target in said_to:
            return 0.2                          # telling the target themselves
        # The statement is only as safe as the least-trusted listener,
        # and only as suspect as the listener closest to the target.
        aud_trust = min([await self._pair_strength(voter, a) for a in said_to])
        target_link = max([await self._pair_strength(a, target) for a in said_to])
        if aud_trust >= 0.6 and aud_trust >= target_link:
            return min(1.0, 0.6 + 0.4 * aud_trust)   # inside their own circle
        if target_link >= 0.6 and target_link > aud_trust:
            return 0.3                          # talking to the target's allies
        return 0.5                              # mixed / weakly-known room

    async def ingest(self, plans: list, week: int) -> None:
        for p in plans:
            if p.confidence < _MIN_CONFIDENCE or p.voter == p.target:
                continue
            said_to = list(getattr(p, "said_to", []) or [])
            cred = await self.credibility(p.voter, p.target, said_to)
            try:
                await self.db.execute(
                    """
                    INSERT INTO vote_plans (week, voter, target, confidence, evidence,
                                            firmness, fallback_target, said_to,
                                            credibility, source_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (week, voter) DO UPDATE
                    SET target = EXCLUDED.target, confidence = EXCLUDED.confidence,
                        evidence = EXCLUDED.evidence, firmness = EXCLUDED.firmness,
                        fallback_target = EXCLUDED.fallback_target,
                        said_to = EXCLUDED.said_to,
                        -- corroborating the SAME target keeps the best credibility
                        credibility = CASE
                            WHEN vote_plans.target = EXCLUDED.target
                            THEN GREATEST(vote_plans.credibility, EXCLUDED.credibility)
                            ELSE EXCLUDED.credibility END,
                        source_hash = EXCLUDED.source_hash, updated_at = now()
                    WHERE vote_plans.target = EXCLUDED.target
                       -- a flip must not be clearly LESS credible than what it replaces
                       OR EXCLUDED.credibility >= vote_plans.credibility - $11
                       -- unless the old statement has gone stale
                       OR vote_plans.updated_at < now() - make_interval(hours => $12)
                    """,
                    week, p.voter, p.target, p.confidence, p.evidence[:500],
                    getattr(p, "firmness", "leaning"),
                    getattr(p, "fallback_target", "") or "",
                    said_to, cred,
                    getattr(p, "source_hash", ""),
                    _CRED_MARGIN, _STALE_HOURS,
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
            SELECT DISTINCT ON (voter) voter, target, firmness, fallback_target,
                   credibility
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
            firm = r["firmness"]
            if r["credibility"] < LOW_CRED:
                firm = "unsure"   # a probable bluff is never displayed as solid
            counts.setdefault(r["target"], []).append((r["voter"], firm))
        return counts

    async def plans(self, week: int) -> list[dict]:
        """Latest ranked plan per voter (same filters as current())."""
        last_evict = await self.db.fetchval(
            "SELECT max(set_at) FROM game_state WHERE role = 'evicted'")
        rows = await self.db.fetch(
            """
            SELECT DISTINCT ON (voter) voter, target, firmness, fallback_target,
                   credibility
            FROM vote_plans
            WHERE week BETWEEN $1 AND $2
              AND ($3::timestamptz IS NULL OR updated_at > $3)
            ORDER BY voter, updated_at DESC
            """,
            max(1, week - 1), week, last_evict,
        )
        evicted = {r["houseguest"] for r in await self.db.fetch(
            "SELECT houseguest FROM game_state WHERE role = 'evicted'")}
        out = []
        for r in rows:
            if r["voter"] in evicted:
                continue
            d = dict(r)
            if d["credibility"] < LOW_CRED:
                d["firmness"] = "unsure"   # probable bluff -> shown shaky
            out.append(d)
        return out

    @staticmethod
    def scenario_board(plans: list[dict], pair: tuple[str, str], saved: str,
                       hoh: str | None = None) -> dict[str, list[tuple[str, str]]]:
        """One Block Buster outcome: `saved` escaped, `pair` face the vote.

        Each voter's vote is their highest-ranked preference that is actually on
        the block — target first, else fallback. The saved houseguest becomes a
        VOTER (that's the twist), with their own stated preference applied. A
        voter with no ranked preference in the pair is listed under '?'.

        The HOH does NOT cast a regular vote — they only break a tie — so they
        are excluded here; use hoh_pick() to see how they'd break one.
        """
        a, b = pair
        board: dict[str, list[tuple[str, str]]] = {a: [], b: [], "?": []}
        for p in plans:
            voter = p["voter"]
            if voter in pair or (hoh and voter == hoh):
                continue                    # on the block / HOH: doesn't vote
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

    @staticmethod
    def hoh_pick(plans: list[dict], pair: tuple[str, str],
                 hoh: str | None) -> str | None:
        """Who the HOH would evict between `pair` if forced to break a tie,
        from their own stated ranked plan. None if they haven't said."""
        if not hoh:
            return None
        for p in plans:
            if p["voter"] != hoh:
                continue
            if p["target"] in pair:
                return p["target"]
            fb = p.get("fallback_target") or ""
            if fb in pair:
                return fb
        return None

    async def remove(self, voter: str, week: int) -> bool:
        result = await self.db.execute(
            "DELETE FROM vote_plans WHERE week = $1 AND voter = $2", week, voter)
        try:
            return int(result.split()[-1]) > 0
        except (ValueError, IndexError, AttributeError):
            return False
