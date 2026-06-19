"""Domain models. Plain dataclasses, no behavior.

`Update` is the unit of ingestion (one feed item). The *Proposal/Change/Event
types are what the LLM extractor emits; trackers consume them after the roster
gate has validated every houseguest name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Update:
    content_hash: str
    source: str            # "rss" | "bluesky"
    author: str
    title: str
    body: str
    link: str
    published_at: datetime  # timezone-aware (UTC)

    @property
    def text(self) -> str:
        """Combined text used for keyword matching / display."""
        if self.body and self.body.strip() and self.body.strip() != self.title.strip():
            return f"{self.title} — {self.body}".strip()
        return self.title.strip()


@dataclass(slots=True)
class AllianceProposal:
    members: list[str]
    status: str            # forming | active | fracturing | dissolved
    confidence: float      # 0..1
    evidence: str
    name: str | None = None


@dataclass(slots=True)
class RelationshipChange:
    houseguests: list[str]  # exactly two, canonical
    kind: str               # allied | conflict | betrayal | showmance_start | showmance_end
    evidence: str


@dataclass(slots=True)
class GameEvent:
    role: str               # hoh | nominee | veto_winner | veto_used_on | evicted | replacement_nominee
    houseguest: str
    confidence: float
    evidence: str


@dataclass(slots=True)
class Extraction:
    alliances: list[AllianceProposal] = field(default_factory=list)
    relationships: list[RelationshipChange] = field(default_factory=list)
    game_events: list[GameEvent] = field(default_factory=list)
