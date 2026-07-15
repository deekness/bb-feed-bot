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
                    "one_sided": {"type": "boolean",
                                  "description": "True ONLY if the text shows the deal is not mutual — "
                                                 "one member trusts it while another is privately playing "
                                                 "or planning against them. Default false."},
                    "one_sided_by": {"type": "array", "items": {"type": "string"},
                                     "description": "If one_sided: the houseguest(s) who do NOT genuinely "
                                                    "mean it — the ones privately playing the others. "
                                                    "E.g. if Drew makes a final 2 with Melody but tells his "
                                                    "real alliance it's fake, this is [\"Drew\"]. Must be "
                                                    "members of this alliance."},
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
                    "firmness": {"type": "string", "enum": ["locked", "leaning", "unsure"],
                                 "description": "How firm the intention reads: 'locked' = definite/"
                                                "'100%', 'leaning' = probable, 'unsure' = wavering/"
                                                "considering. Default 'leaning'."},
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
            "- GAME STATE IS FOR COMPLETED, CONFIRMED EVENTS ONLY. Record a game "
            "event only if the text says it HAS HAPPENED. Never record plans, "
            "intentions, predictions, rumors, conditionals or hypotheticals. "
            "'Dee plans to put up Melody', 'Melody is the renom IF someone wins "
            "veto', 'they're thinking about backdooring X', 'Dee wants X out' are "
            "all PLANS — do not record them. Only 'Dee named Melody as the "
            "replacement nominee' (it happened) counts.\n"
            "- Specifically: 'replacement_nominee' is valid ONLY after the veto "
            "ceremony actually occurred and the veto was actually used. If the "
            "veto comp or ceremony has not happened yet, there is no replacement "
            "nominee — omit it entirely.\n"
            "- 'nominee' means someone actually nominated at the nomination "
            "ceremony, not someone people are discussing nominating.\n"
            "- A vote plan requires the voter stating or clearly implying who they "
            "will vote to evict this week — not who they dislike. Set 'firmness' to "
            "'locked' only for definite statements ('100%', 'for sure'), 'unsure' when "
            "they are wavering, else 'leaning'.\n"
            "- RANKED VOTES: this season three houseguests are nominated and one "
            "escapes eviction night via the Block Buster comp, so voters often rank: "
            "'I want Taylor out, but if she saves herself I vote Ashley'. Put the "
            "first choice in 'target' and the conditional second in 'fallback_target'. "
            "A plain 'voting X' statement is target only, no fallback.\n"
            "- AUDIENCE: when the update names who the voter was talking to, fill "
            "'said_to' with those houseguests. Houseguests bluff — a vote stated to "
            "the target or the target's allies is often theatre, so the audience is "
            "real signal. Never infer listeners who aren't named.\n"
            "- Mark an alliance 'one_sided' ONLY when the text clearly shows the deal "
            "isn't mutual (one side trusts it while another schemes against them). When "
            "in doubt, leave it false. When you do mark it, ALWAYS fill 'one_sided_by' "
            "with whoever is doing the playing — the deal being fake matters far less "
            "than who is faking it.\n"
            "- A final-2 or final-3 deal IS an alliance — record it with just those two "
            "or three members; do not fold it into a larger group.\n"
            "- NAMES: only set 'name' when the members you are listing are the ones "
            "who call THEMSELVES that. Houseguests constantly discuss OTHER "
            "people's alliances — if Drew and Devens are talking about a group "
            "called 'The Red Corner', that name belongs to the group they are "
            "TALKING ABOUT, not to Drew and Devens. Attributing an overheard name "
            "to the speakers is a serious error: leave 'name' null unless the "
            "members are clearly naming their own alliance.\n"
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

    def _validate(self, data, updates: list) -> Extraction:
        # Forced tool-use gives the model a schema, but it does not always honour
        # it: it occasionally emits a bare STRING where an object is required.
        # One such item used to raise "'str' object has no attribute 'get'" and
        # abort the ENTIRE ingest cycle — silently discarding every alliance,
        # relationship, game event and vote plan in that batch. Degrade to
        # "found nothing" instead of taking the loop down.
        if not isinstance(data, dict):
            log.warning("extraction: payload was %s, not an object",
                        type(data).__name__)
            return Extraction()
        result = Extraction()

        def src(item: dict) -> str:
            try:
                if not isinstance(item, dict):
                    return ""
                i = int(item.get("source_index", 0))
                if 1 <= i <= len(updates):
                    return updates[i - 1].content_hash
            except (TypeError, ValueError):
                pass
            return updates[0].content_hash if updates else ""

        _alliances = data.get("alliances") or []
        if not isinstance(_alliances, list):
            log.warning("extraction: alliances was %s, not a list — skipping",
                        type(_alliances).__name__)
            _alliances = []
        for a in _alliances:
            if not isinstance(a, dict):
                log.warning("extraction: skipping non-object alliance %r", a)
                continue
            members = self.roster.resolve_all(a.get("members", []) or [])
            if len(members) < 2:  # an alliance needs >= 2 real houseguests
                continue
            result.alliances.append(AllianceProposal(
                members=members,
                status=str(a.get("status", "forming")),
                confidence=_clamp(a.get("confidence", 0.5)),
                evidence=str(a.get("evidence", ""))[:500],
                name=_clean_name(a.get("name")),
                one_sided=bool(a.get("one_sided", False)),
                one_sided_by=[m for m in self.roster.resolve_all(
                    list(a.get("one_sided_by") or [])) if m in members],
                source_hash=src(a),
            ))

        _relationship_changes = data.get("relationship_changes") or []
        if not isinstance(_relationship_changes, list):
            log.warning("extraction: relationship_changes was %s, not a list — skipping",
                        type(_relationship_changes).__name__)
            _relationship_changes = []
        for r in _relationship_changes:
            if not isinstance(r, dict):
                log.warning("extraction: skipping non-object relationship %r", r)
                continue
            pair = self.roster.resolve_all(r.get("houseguests", []) or [])
            if len(pair) != 2:
                continue
            result.relationships.append(RelationshipChange(
                houseguests=pair, kind=str(r.get("kind", "")),
                evidence=str(r.get("evidence", ""))[:500],
            ))

        _game_state = data.get("game_state") or []
        if not isinstance(_game_state, list):
            log.warning("extraction: game_state was %s, not a list — skipping",
                        type(_game_state).__name__)
            _game_state = []
        for g in _game_state:
            if not isinstance(g, dict):
                log.warning("extraction: skipping non-object game event %r", g)
                continue
            hg = self.roster.resolve(g.get("houseguest"))
            if not hg:
                continue
            result.game_events.append(GameEvent(
                role=str(g.get("role", "")), houseguest=hg,
                confidence=_clamp(g.get("confidence", 0.5)),
                evidence=str(g.get("evidence", ""))[:500],
                source_hash=src(g),
            ))

        _vote_plans = data.get("vote_plans") or []
        if not isinstance(_vote_plans, list):
            log.warning("extraction: vote_plans was %s, not a list — skipping",
                        type(_vote_plans).__name__)
            _vote_plans = []
        for v in _vote_plans:
            if not isinstance(v, dict):
                log.warning("extraction: skipping non-object vote plan %r", v)
                continue
            voter = self.roster.resolve(v.get("voter"))
            target = self.roster.resolve(v.get("target"))
            if not voter or not target or voter == target:
                continue
            firmness = str(v.get("firmness", "leaning")).lower()
            if firmness not in ("locked", "leaning", "unsure"):
                firmness = "leaning"
            fb = self.roster.resolve(str(v.get("fallback_target") or "")) or ""
            if fb in (voter, target):
                fb = ""          # a fallback must be a distinct third person
            aud = [m for m in self.roster.resolve_all(list(v.get("said_to") or []))
                   if m != voter]
            result.vote_plans.append(VotePlan(
                voter=voter, target=target,
                fallback_target=fb,
                said_to=aud,
                confidence=_clamp(v.get("confidence", 0.5)),
                evidence=str(v.get("evidence", ""))[:500],
                firmness=firmness,
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
