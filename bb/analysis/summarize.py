"""Neutral summarization of feed activity.

Reads updates from the DB for a time window (the DB is the queue) and produces
Discord embeds. Uses the LLM when available, with a deterministic pattern
fallback otherwise.

Neutrality: importance scoring is based purely on EVENT TYPE (keywords), never
on who is involved. The LLM prompt forbids favoritism. No houseguest is
weighted differently from any other.
"""
from __future__ import annotations

import logging
from datetime import datetime

import discord

from ..llm import LLM
from ..models import Update

log = logging.getLogger("bb.analysis.summarize")

# Event-type keywords only — identity-neutral by construction.
_IMPORTANCE = {
    4: ("evicted", "eviction", "self-evict", "expelled", "quit", "winner of"),
    3: ("hoh", "head of household", "power of veto", "veto", "nominat", "backdoor",
        "blindside", "fight", "argument", "showmance", "kiss"),
    2: ("alliance", "target", "campaign", "vote", "deal", "crying", "blowup"),
    1: ("strategy", "talk", "conversation"),
}

URGENT_KEYWORDS = (
    "evicted", "eviction", "wins hoh", "won hoh", "wins veto", "won veto",
    "wins the veto", "self-evict", "expelled", "quit the game", "backdoor",
    "blindside", "removed from the house", "medical",
)


def importance(update: Update) -> int:
    text = update.text.lower()
    score = 1
    for value, words in _IMPORTANCE.items():
        if any(w in text for w in words):
            score = max(score, value)
    return min(score, 5)


def is_urgent(update: Update) -> bool:
    text = update.text.lower()
    return any(k in text for k in URGENT_KEYWORDS)


class Summarizer:
    def __init__(self, llm: LLM, tz):
        self.llm = llm
        self.tz = tz

    # --- hourly digest ------------------------------------------------------
    async def hourly(self, updates: list[Update], hour_label: str) -> list[discord.Embed]:
        if not updates:
            return [self._quiet_embed(hour_label)]
        if self.llm.available:
            embed = await self._llm_digest(updates, hour_label)
            if embed:
                return [embed]
        return [self._pattern_digest(updates, hour_label)]

    # --- on-demand "what's happening" --------------------------------------
    async def whats_happening(self, updates: list[Update]) -> discord.Embed:
        if not updates:
            return discord.Embed(
                title="Nothing's happening",
                description="No updates in the last 24 hours.",
                color=0x95A5A6,
            )
        top = sorted(updates, key=importance, reverse=True)[:5]
        if self.llm.available:
            embed = await self._llm_whats_happening(top, len(updates))
            if embed:
                return embed
        return self._pattern_whats_happening(top, len(updates))

    # --- LLM paths ----------------------------------------------------------
    async def _llm_digest(self, updates: list[Update], hour_label: str) -> discord.Embed | None:
        body = "\n".join(f"- {u.text}" for u in sorted(updates, key=lambda u: u.published_at))
        system = (
            "You are a neutral Big Brother live-feed reporter. Summarize the hour "
            "factually and even-handedly. Do NOT favor, root for, or criticize any "
            "houseguest, and do not opine on who is playing well or 'deserves' to win "
            "beyond what houseguests themselves say and do. Be concise."
        )
        user = (
            f"Summarize what happened this hour ({hour_label}) in 3-5 short sentences, "
            f"in chronological order:\n\n{body}"
        )
        text = await self.llm.text(system, user, max_tokens=700, temperature=0.4)
        if not text:
            return None
        embed = discord.Embed(
            title=f"House Summary — {hour_label}",
            description=text, color=0x5865F2, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"{len(updates)} updates this hour")
        return embed

    async def _llm_whats_happening(self, top: list[Update], total: int) -> discord.Embed | None:
        body = "\n".join(f"- {u.text}" for u in top)
        system = (
            "You are a neutral Big Brother reporter catching someone up after a day "
            "away. Be factual and even-handed — no favoritism toward any houseguest."
        )
        user = (
            "From these recent updates, give the 5 most important current happenings "
            "as short bullet points (one sentence each), then a one-line overall "
            f"summary. Updates:\n\n{body}"
        )
        text = await self.llm.text(system, user, max_tokens=800, temperature=0.4)
        if not text:
            return None
        embed = discord.Embed(
            title="What's happening right now",
            description=text, color=0xFF6B35, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"Based on {total} updates in the last 24h")
        return embed

    # --- pattern fallbacks --------------------------------------------------
    def _pattern_digest(self, updates: list[Update], hour_label: str) -> discord.Embed:
        top = sorted(updates, key=importance, reverse=True)[:8]
        lines = [f"• {self._trim(u.text)}" for u in top]
        embed = discord.Embed(
            title=f"House Summary — {hour_label}",
            description="\n".join(lines), color=0x9B59B6,
            timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"{len(updates)} updates this hour")
        return embed

    def _pattern_whats_happening(self, top: list[Update], total: int) -> discord.Embed:
        lines = [f"{i}. {self._trim(u.text)}" for i, u in enumerate(top, 1)]
        embed = discord.Embed(
            title="What's happening right now",
            description="\n".join(lines), color=0xFF6B35,
            timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"Based on {total} updates in the last 24h")
        return embed

    def _quiet_embed(self, hour_label: str) -> discord.Embed:
        return discord.Embed(
            title=f"House Summary — {hour_label}",
            description="A quiet hour — no significant updates on the feeds.",
            color=0x95A5A6, timestamp=datetime.now(self.tz),
        )

    @staticmethod
    def _trim(text: str, limit: int = 180) -> str:
        return text if len(text) <= limit else text[: limit - 3] + "..."
