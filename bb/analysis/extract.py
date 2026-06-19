"""Structured extraction of house dynamics from a batch of feed updates.

This replaces the old regex + blacklist approach. The model proposes
alliances / relationship changes / game-state facts via a forced tool call
(reliable JSON), and EVERY houseguest name is then validated against the
roster. Anything that does not resolve is discarded, so "random word became
an alliance" cannot happen.

Neutrality: the prompt instructs the model to report only what is evidenced,
with a supporting quote, and to treat all houseguests equally. There is no
per-houseguest handling here or downstream.
"""
from __future__ import annotations

import logging

from ..llm import LLM
from ..models import AllianceProposal, Extraction, GameEvent, RelationshipChange
from ..roster import Roster

log = logging.getLogger("bb.analysis.extract")

_TOOL_NAME = "record_house_dynamics"
_TOOL_DESCRIPTION = (
    "Record ONLY the alliances, relationship changes, and game-state facts that "
    "are directly stated or clearly evidenced in the provided feed updates. Use "
    "empty arrays when nothing qualifies. Never infer beyond the text."
)

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
                },
                "required": ["members", "status", "confidence", "evidence"],
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
                },
                "required": ["houseguests", "kind", "evidence"],
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
                },
                "required": ["role", "houseguest", "confidence", "evidence"],
            },
        },
    },
    "required": ["alliances", "relationship_changes", "game_state"],
}


class Extractor:
    def __init__(self, llm: LLM, roster: Roster):
        self.llm = llm
        self.roster = roster

    async def extract(self, updates: list) -> Extraction:
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
            "- An alliance requires an explicit agreement or working relationship in "
            "the text — not merely two people talking. If unsure, lower the confidence "
            "or omit it.\n"
            "- Only give an alliance a name if the houseguests themselves named it.\n"
            "- Every item must include a short supporting quote in 'evidence'. If you "
            "cannot quote support, do not include the item.\n"
            "- Return empty arrays rather than guessing."
        )
        user = (
            "Extract the alliances, relationship changes, and game-state facts that "
            "are evidenced in these updates:\n\n" + "\n".join(lines)
        )

        data = await self.llm.structured(
            system, user, tool_name=_TOOL_NAME, tool_description=_TOOL_DESCRIPTION,
            schema=_SCHEMA, max_tokens=2000,
        )
        if not data:
            return Extraction()
        return self._validate(data)

    def _validate(self, data: dict) -> Extraction:
        result = Extraction()

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
            ))

        log.info("extraction: %d alliances, %d relationship changes, %d game events",
                 len(result.alliances), len(result.relationships), len(result.game_events))
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
