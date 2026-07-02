"""Neutral summarization of feed activity.

Reads updates from the DB for a time window (the DB is the queue) and produces
Discord embeds. Uses the LLM when available, with a deterministic pattern
fallback otherwise.

Every LLM path receives a short CURRENT HOUSE STATE block (week, HOH, noms,
veto, active alliances) so summaries are anchored to the game, not free-floating
prose. The daily recap is map-reduce: it is built from the day's STORED hourly
summaries (plus game events), not from a lossy top-5 of raw updates.

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

_NEUTRALITY = (
    "You are a neutral Big Brother live-feed reporter. Be factual and "
    "even-handed. Do NOT favor, root for, or criticize any houseguest, and do "
    "not opine on who is playing well or 'deserves' to win beyond what "
    "houseguests themselves say and do."
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
    async def hourly(self, updates: list[Update], hour_label: str,
                     house_context: str = "") -> list[discord.Embed]:
        """Returns [] for a quiet hour — the caller posts nothing rather than
        spamming the channel with 'quiet hour' embeds overnight."""
        if not updates:
            return []
        if self.llm.available:
            embed = await self._llm_digest(updates, hour_label, house_context)
            if embed:
                return [embed]
        return [self._pattern_digest(updates, hour_label)]

    # --- on-demand "what's happening" --------------------------------------
    async def whats_happening(self, updates: list[Update],
                              house_context: str = "") -> discord.Embed:
        if not updates:
            return discord.Embed(
                title="Nothing's happening",
                description="No updates in the last 24 hours.",
                color=0x95A5A6,
            )
        top = sorted(updates, key=importance, reverse=True)[:15]
        if self.llm.available:
            embed = await self._llm_whats_happening(top, len(updates), house_context)
            if embed:
                return embed
        return self._pattern_whats_happening(top[:5], len(updates))

    # --- daily recap (map-reduce over stored hourly summaries) --------------
    async def daily_recap(self, hourly_summaries: list[dict],
                          fallback_updates: list[Update], day_number: int,
                          house_context: str = "") -> discord.Embed:
        """Build the day recap from stored hourly digests (complete coverage of
        the day) instead of a top-5 of raw updates. Falls back to the old
        whats_happening path if no hourly summaries exist yet."""
        if not hourly_summaries or not self.llm.available:
            embed = await self.whats_happening(fallback_updates, house_context)
            embed.title = f"Day {day_number} Recap"
            return embed

        blocks = []
        for s in hourly_summaries:
            label = s["period_end"].astimezone(self.tz).strftime("%I %p").lstrip("0")
            blocks.append(f"[{label}] ({s['update_count']} updates)\n{s['content']}")
        body = "\n\n".join(blocks)
        total = sum(s["update_count"] for s in hourly_summaries)

        system = _NEUTRALITY
        user = (
            f"{self._ctx(house_context)}"
            "Below are the hour-by-hour summaries for the last day in the Big "
            "Brother house. Write a day recap:\n"
            "1. A short paragraph capturing the day's main storyline(s).\n"
            "2. 4-7 bullet points of the key developments, chronological.\n"
            "Cover the whole day — do not drop threads that only appear in one "
            f"hour.\n\nHOURLY SUMMARIES:\n\n{body}"
        )
        text = await self.llm.text(system, user, max_tokens=1000, temperature=0.4)
        if not text:
            embed = await self.whats_happening(fallback_updates, house_context)
            embed.title = f"Day {day_number} Recap"
            return embed
        embed = discord.Embed(
            title=f"Day {day_number} Recap",
            description=text[:4000], color=0xFF6B35, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"Built from {len(hourly_summaries)} hourly summaries • {total} updates")
        return embed

    # --- LLM paths ----------------------------------------------------------
    async def _llm_digest(self, updates: list[Update], hour_label: str,
                          house_context: str) -> discord.Embed | None:
        body = "\n".join(f"- {u.text}" for u in sorted(updates, key=lambda u: u.published_at))
        system = _NEUTRALITY + " Be concise."
        user = (
            f"{self._ctx(house_context)}"
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

    async def _llm_whats_happening(self, top: list[Update], total: int,
                                   house_context: str) -> discord.Embed | None:
        body = "\n".join(f"- {u.text}" for u in top)
        system = _NEUTRALITY + " You are catching someone up after a day away."
        user = (
            f"{self._ctx(house_context)}"
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

    @staticmethod
    def _ctx(house_context: str) -> str:
        if not house_context:
            return ""
        return f"CURRENT HOUSE STATE (for context, do not re-report):\n{house_context}\n\n"

    # --- pattern fallbacks --------------------------------------------------
    def _pattern_digest(self, updates: list[Update], hour_label: str) -> discord.Embed:
        top = sorted(updates, key=importance, reverse=True)[:8]
        lines = [f"• {self._linked(u)}" for u in top]
        embed = discord.Embed(
            title=f"House Summary — {hour_label}",
            description="\n".join(lines)[:4000], color=0x9B59B6,
            timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"{len(updates)} updates this hour")
        return embed

    def _pattern_whats_happening(self, top: list[Update], total: int) -> discord.Embed:
        lines = [f"{i}. {self._linked(u)}" for i, u in enumerate(top, 1)]
        embed = discord.Embed(
            title="What's happening right now",
            description="\n".join(lines)[:4000], color=0xFF6B35,
            timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"Based on {total} updates in the last 24h")
        return embed

    def _linked(self, u: Update) -> str:
        text = self._trim(u.text)
        return f"[{text}]({u.link})" if u.link else text

    @staticmethod
    def _trim(text: str, limit: int = 180) -> str:
        return text if len(text) <= limit else text[: limit - 3] + "..."
