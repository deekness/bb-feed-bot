"""Structured extraction of house dynamics from a batch of feed updates.

This replaces the old regex + blacklist approach. The model proposes
alliances / relationship changes / game-state facts via a forced tool call
(reliable JSON), and EVERY houseguest name is then validated against the
roster. Anything that does not resolve is discarded, so "random word became
an alliance" cannot happen.

Context: per-poll batches are tiny (often 1-3 items), so the model also gets
(a) the current game state + active alliances, and (b) the last few
already-processed updates, marked CONTEXT ONLY. It extracts facts only from
the NEW updates; context exists purely for disambiguation.

Attribution: each extracted item carries a source_index pointing at the
numbered NEW update it came from, so evidence rows link to the right update.

Neutrality: the prompt instructs the model to report only what is evidenced,
with a supporting quote, and to treat all houseguests equally. There is no
per-houseguest handling here or downstream.
"""
from __future__ import annotations

import logging

from ..llm import LLM
from ..models import AllianceProposal, Extraction, GameEvent, RelationshipChange, VotePlan
from ..roster import Roster

log = logging.getLogger("bb.analysis.extract")

_TOOL_NAME = "record_house_dynamics"
_TOOL_DESCRIPTION = (
    "Record ONLY the alliances, relationship changes, and game-state facts that "
    "are directly stated or clearly evidenced in the NEW feed updates. Use "
    "empty arrays when nothing qualifies. Never infer beyond the text."
)

_SRC_IDX = {
    "type": "integer",
    "description": "The number of the NEW update this item is evidenced by.",
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "alliances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"],
                             "description": "Only if the houseguests explicitly named it; else null."},
                    "members": {"type": "array", "items": {"type": "string"},
                                "description": "Houseguest first names. Only people who are clearly part of it."},
                    "status": {"type": "string",
                               "enum": ["forming", "active", "fracturing", "dissolved"]},
                    "confidence": {"type": "number",
                                   "description": "0..1: how strongly the updates support this alliance."},
                    "evidence": {"type": "string",
                                 "description": "Short quote/paraphrase from the updates supporting this."},
                    "source_index": _SRC_IDX,
                },
                "required": ["members", "status", "confidence", "evidence", "source_index"],
            },
        },
        "relationship_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "houseguests": {"type": "array", "items": {"type": "string"},
                                    "description": "Exactly the two houseguests involved."},
                    "kind": {"type": "string",
                             "enum": ["allied", "conflict", "betrayal",
                                      "showmance_start", "showmance_end"]},
                    "evidence": {"type": "string"},
                    "source_index": _SRC_IDX,
                },
                "required": ["houseguests", "kind", "evidence", "source_index"],
            },
        },
        "game_state": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string",
                             "enum": ["hoh", "nominee", "veto_winner",
                                      "veto_used_on", "evicted", "replacement_nominee"]},
                    "houseguest": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                    "source_index": _SRC_IDX,
                },
                "required": ["role", "houseguest", "confidence", "evidence", "source_index"],
            },
        },
        "vote_plans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "voter": {"type": "string",
                              "description": "The houseguest casting the vote."},
                    "target": {"type": "string",
                               "description": "Who the voter says they will vote to EVICT."},
                    "confidence": {"type": "number",
                                   "description": "0..1: only stated/clearly implied vote intentions."},
                    "evidence": {"type": "string"},
                    "source_index": _SRC_IDX,
                },
                "required": ["voter", "target", "confidence", "evidence", "source_index"],
            },
        },
    },
    "required": ["alliances", "relationship_changes", "game_state", "vote_plans"],
}


class Extractor:
    def __init__(self, llm: LLM, roster: Roster):
        self.llm = llm
        self.roster = roster

    async def extract(self, updates: list, context_updates: list | None = None,
                      house_context: str = "", episode_airing: bool = False) -> Extraction:
        """Extract from `updates` (NEW). `context_updates` are recent,
        already-processed items shown for disambiguation only.
        `house_context` is a short current-state block (week, HOH, noms,
        active alliances) built by the caller."""
        if not self.llm.available or not updates or self.roster.is_empty:
            return Extraction()

        roster_str = ", ".join(self.roster.names)
        lines = [f"{i}. {u.text}" for i, u in enumerate(updates, 1)]

        system = (
            "You are a neutral Big Brother live-feed analyst. You extract factual "
            "house dynamics from update text. You are strictly even-handed: you do "
            "not favor, root for, or disparage any houseguest, and you never "
            "speculate about who 'deserves' anything.\n\n"
            f"The ONLY valid houseguests this season are: {roster_str}. Never output "
            "a name that is not in this list. If a name is ambiguous, omit it.\n\n"
            "Rules:\n"
            "- Extract ONLY from the NEW updates. The CURRENT HOUSE STATE and "
            "CONTEXT sections exist purely to help you interpret the new text — "
            "never re-report facts that appear only there.\n"
            "- An alliance requires an explicit agreement or working relationship in "
            "the text — not merely two people talking. If unsure, lower the confidence "
            "or omit it.\n"
            "- Only give an alliance a name if the houseguests themselves named it.\n"
            "- Every item must include a short supporting quote in 'evidence' and the "
            "source_index of the NEW update it came from. If you cannot quote support, "
            "do not include the item.\n"
            "- A vote plan requires the voter stating or clearly implying who they "
            "will vote to evict this week — not who they dislike.\n"
            "- Return empty arrays rather than guessing."
        )
        if episode_airing:
            system += (
                "\n\nIMPORTANT: A pre-recorded TV episode is airing right now. Some "
                "updates may be people reacting to OLD events shown on the episode, "
                "not live feed events. If an update reads like episode commentary or "
                "recaps something already in the CURRENT HOUSE STATE, do not extract "
                "game-state facts or vote plans from it, or use very low confidence."
            )

        parts = []
        if house_context:
            parts.append(f"CURRENT HOUSE STATE (context only):\n{house_context}")
        if context_updates:
            ctx = "\n".join(f"- {u.text}" for u in context_updates)
            parts.append(f"RECENT UPDATES ALREADY PROCESSED (context only):\n{ctx}")
        parts.append(
            "NEW updates — extract alliances, relationship changes, and game-state "
            "facts evidenced in these:\n" + "\n".join(lines)
        )
        user = "\n\n".join(parts)

        data = await self.llm.structured(
            system, user, tool_name=_TOOL_NAME, tool_description=_TOOL_DESCRIPTION,
            schema=_SCHEMA, max_tokens=2000,
        )
        if not data:
            return Extraction()
        return self._validate(data, updates)

    def _validate(self, data: dict, updates: list) -> Extraction:
        result = Extraction()

        def src(item: dict) -> str:
            try:
                i = int(item.get("source_index", 0))
                if 1 <= i <= len(updates):
                    return updates[i - 1].content_hash
            except (TypeError, ValueError):
                pass
            return updates[0].content_hash if updates else ""

        for a in data.get("alliances", []) or []:
            members = self.roster.resolve_all(a.get("members", []) or [])
            if len(members) < 2:  # an alliance needs >= 2 real houseguests
                continue
            result.alliances.append(AllianceProposal(
                members=members,
                status=str(a.get("status", "forming")),
                confidence=_clamp(a.get("confidence", 0.5)),
                evidence=str(a.get("evidence", ""))[:500],
                name=_clean_name(a.get("name")),
                source_hash=src(a),
            ))

        for r in data.get("relationship_changes", []) or []:
            pair = self.roster.resolve_all(r.get("houseguests", []) or [])
            if len(pair) != 2:
                continue
            result.relationships.append(RelationshipChange(
                houseguests=pair, kind=str(r.get("kind", "")),
                evidence=str(r.get("evidence", ""))[:500],
            ))

        for g in data.get("game_state", []) or []:
            hg = self.roster.resolve(g.get("houseguest"))
            if not hg:
                continue
            result.game_events.append(GameEvent(
                role=str(g.get("role", "")), houseguest=hg,
                confidence=_clamp(g.get("confidence", 0.5)),
                evidence=str(g.get("evidence", ""))[:500],
                source_hash=src(g),
            ))

        for v in data.get("vote_plans", []) or []:
            voter = self.roster.resolve(v.get("voter"))
            target = self.roster.resolve(v.get("target"))
            if not voter or not target or voter == target:
                continue
            result.vote_plans.append(VotePlan(
                voter=voter, target=target,
                confidence=_clamp(v.get("confidence", 0.5)),
                evidence=str(v.get("evidence", ""))[:500],
                source_hash=src(v),
            ))

        log.info("extraction: %d alliances, %d relationship changes, %d game events, %d vote plans",
                 len(result.alliances), len(result.relationships), len(result.game_events),
                 len(result.vote_plans))
        return result


def _clamp(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


def _clean_name(name) -> str | None:
    if not name:
        return None
    s = str(name).strip()
    return s if s and s.lower() not in ("null", "none", "unnamed") else None
