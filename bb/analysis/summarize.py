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
import re
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
    "wins the veto", "self-evict", "self-evicted", "expelled", "ejected",
    "quit the game", "walked out", "removed from the house", "removed from the game",
    "backdoor", "backdoored", "blindside", "blindsided", "medical",
    "returns to the house", "re-enters", "battle back", "double eviction",
    "triple eviction", "diamond veto", "coup", "pandora",
)

_NEUTRALITY = (
    "Never include URLs or markdown links in your output. "
    "The updates you are given are the ONLY data available; never ask the "
    "reader to provide feed details, and never say you lack information for "
    "the hour. If the material is thin, write a brief wry line in your reporter "
    "voice about the quiet rather than requesting more data. "
    "You are a neutral Big Brother live-feed reporter. Be factual and "
    "even-handed. Do NOT favor, root for, or criticize any houseguest, and do "
    "not opine on who is playing well or 'deserves' to win beyond what "
    "houseguests themselves say and do."
)


_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_BARE_URL = re.compile(r"https?://\S+")


def strip_links(text: str) -> str:
    """Remove markdown links (keeping their label) and bare URLs. Bot outputs
    are link-free by policy; raw update texts can carry URLs and the LLM will
    happily echo them, so LLM output is scrubbed too."""
    text = _MD_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def sentence_clamp(text: str, limit: int) -> str:
    """Fit text into `limit` chars without ever cutting mid-sentence. Prefers
    the last sentence boundary before the limit; falls back to the last
    whitespace. No trailing ellipsis — output always reads complete."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for stop in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        idx = cut.rfind(stop)
        if idx >= int(limit * 0.4):
            return cut[: idx + 1].rstrip()
    idx = cut.rfind(" ")
    return (cut[:idx] if idx > 0 else cut).rstrip()


def fit_whole_items(items: list[str], budget: int) -> list[str]:
    """Take items in order while they fit the budget WHOLE — never truncating
    an item. If any are left over, append a one-line '+N more' marker."""
    out: list[str] = []
    used = 0
    for i, item in enumerate(items):
        cost = len(item) + 1
        if used + cost > budget:
            remaining = len(items) - i
            out.append(f"*…and {remaining} more update{'s' if remaining != 1 else ''}*")
            break
        out.append(item)
        used += cost
    return out


def importance(update: Update) -> int:
    text = update.text.lower()
    score = 1
    for value, words in _IMPORTANCE.items():
        if any(w in text for w in words):
            score = max(score, value)
    return min(score, 5)


def urgent_keyword(update: Update) -> str | None:
    """The first urgent keyword present in the update, or None. The keyword
    itself is used by the caller as a cooldown key so one real-world event
    reported by multiple sources fires one Breaking post, not several."""
    text = update.text.lower()
    for k in URGENT_KEYWORDS:
        if k in text:
            return k
    return None


def is_urgent(update: Update) -> bool:
    return urgent_keyword(update) is not None


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

    # --- episode recap (grouped digest of an aired episode's chatter) -------
    async def episode_recap(self, updates: list[Update], label: str,
                            house_context: str = "") -> discord.Embed | None:
        """Recap ONE aired episode from the feed/viewer posts during and just
        after it. Episode chatter is exactly what we want here (people
        live-posting the broadcast), so there is no feed-gating. Grouped-bullet
        format, on the recap model."""
        if not updates:
            return None
        body = "\n".join(f"- {u.text}"
                          for u in sorted(updates, key=lambda u: u.published_at))
        system = _NEUTRALITY + " Be concise."
        user = (
            f"{self._ctx(house_context)}"
            f"Below are feed updates and viewer posts from during and just after "
            f"tonight's Big Brother episode ({label}). Summarize WHAT HAPPENED ON "
            "THE EPISODE as scannable bullets GROUPED BY TOPIC. Format rules:\n"
            "- Short bold topic headers on their own line, e.g. **Safety "
            "Competition**, **HOH Competition**, **Nomination Ceremony**, "
            "**Eviction**, **Twist**, **Notable Moments** (only topics that "
            "appear).\n"
            "- Under each header, concise bullets starting with '- ', one line each.\n"
            "- Blank line between groups.\n"
            "- Report confirmed on-screen events as fact; clearly mark anything "
            "that is only fan speculation or reaction as such.\n"
            "- No intro or closing paragraph — start at the first header.\n\n"
            f"UPDATES:\n{body}"
        )
        text = await self.llm.text(system, user, max_tokens=1200, heavy=True)
        if not text:
            return None
        embed = discord.Embed(
            title=f"📺 Episode Recap — {label}",
            description=sentence_clamp(strip_links(text), 4000),
            color=0xF1C40F, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"{len(updates)} updates during & after the episode")
        return embed

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
            "1. A short narrative paragraph on the day's main storyline(s). Where "
            "the CURRENT HOUSE STATE shows alliances, relationship beats "
            "(especially betrayals), or a vote board, use them to frame WHY the "
            "day mattered — how alliances shifted or who turned on whom — rather "
            "than just listing events.\n"
            "2. 4-7 bullet points of the key developments, chronological.\n"
            "Cover the whole day — do not drop threads that only appear in one "
            f"hour.\n\nHOURLY SUMMARIES:\n\n{body}"
        )
        text = await self.llm.text(system, user, max_tokens=1000, temperature=0.4, heavy=True)
        if not text:
            embed = await self.whats_happening(fallback_updates, house_context)
            embed.title = f"Day {day_number} Recap"
            return embed
        embed = discord.Embed(
            title=f"Day {day_number} Recap",
            description=sentence_clamp(strip_links(text), 4000), color=0xFF6B35, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"Built from {len(hourly_summaries)} hourly summaries • {total} updates")
        return embed

    # --- /ask: natural-language Q&A over the archive -------------------------
    async def ask(self, question: str, matches: list[Update],
                  recent_dailies: list[dict], house_context: str = "") -> discord.Embed:
        if not self.llm.available:
            return discord.Embed(
                title="Ask", color=0x95A5A6,
                description="LLM is off — /ask needs it. Try /summary instead.")
        if not matches and not recent_dailies:
            return discord.Embed(
                title=f"❓ {question[:230]}", color=0x95A5A6,
                description="I couldn't find anything in the feed archive about that.")

        parts = [self._ctx(house_context)] if house_context else []
        if recent_dailies:
            days = "\n\n".join(
                f"[{d['period_end'].astimezone(self.tz).strftime('%b %d')}]\n{d['content'][:1200]}"
                for d in recent_dailies[-5:])
            parts.append(f"RECENT DAILY RECAPS (background):\n{days}")
        if matches:
            found = "\n".join(
                f"- [{u.published_at.astimezone(self.tz).strftime('%b %d %I:%M %p')}] {self._trim(u.text, 300)}"
                for u in matches[:40])
            parts.append(f"FEED UPDATES MATCHING THE QUESTION (newest first):\n{found}")
        parts.append(
            f"QUESTION: {question}\n\n"
            "Answer using only the material above. Be specific about who/when. "
            "If the archive doesn't fully answer it, say what is and isn't known. "
            "Stay neutral toward every houseguest. 2-6 sentences or a short list.")

        text = await self.llm.text(_NEUTRALITY, "\n\n".join(parts),
                                   max_tokens=800, temperature=0.3)
        embed = discord.Embed(
            title=f"❓ {question[:230]}",
            description=sentence_clamp(strip_links(text), 4000) if text else "Couldn't produce an answer — try rewording.",
            color=0x1ABC9C, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"Searched archive: {len(matches)} matching updates")
        return embed

    # --- weekly recap (reduce over stored daily summaries) -------------------
    async def weekly_recap(self, dailies: list[dict], week_number: int,
                           house_context: str = "") -> discord.Embed:
        if not dailies:
            return discord.Embed(
                title=f"Week {week_number} Recap", color=0x95A5A6,
                description="No daily recaps stored for that week yet.")
        if not self.llm.available:
            body = "\n\n".join(
                f"**{d['period_end'].astimezone(self.tz).strftime('%A %b %d')}**\n{d['content'][:500]}"
                for d in dailies)
            return discord.Embed(title=f"Week {week_number} Recap",
                                 description=sentence_clamp(body, 4000), color=0x8E44AD)
        blocks = "\n\n".join(
            f"[{d['period_end'].astimezone(self.tz).strftime('%A %b %d')}]\n{d['content']}"
            for d in dailies)
        user = (
            f"{self._ctx(house_context)}"
            f"Below are the daily recaps for week {week_number} in the Big Brother "
            "house. Write the week's story:\n"
            "1. A paragraph on the week's arc (HOH -> noms -> veto -> eviction if "
            "known), using the CURRENT HOUSE STATE — alliances, relationship beats, "
            "betrayals, the vote board — to explain the power shifts, not just the "
            "sequence of comps.\n"
            "2. 5-8 bullets of key developments, chronological.\n"
            f"3. One line on where things stand going into next week.\n\nDAILY RECAPS:\n\n{blocks}"
        )
        text = await self.llm.text(_NEUTRALITY, user, max_tokens=1200, temperature=0.4, heavy=True)
        embed = discord.Embed(
            title=f"📆 Week {week_number} Recap",
            description=sentence_clamp(strip_links(text), 4000) if text else "Recap generation failed.",
            color=0x8E44AD, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"Built from {len(dailies)} daily recaps")
        return embed

    # --- LLM paths ----------------------------------------------------------
    async def _llm_digest(self, updates: list[Update], hour_label: str,
                          house_context: str) -> discord.Embed | None:
        body = "\n".join(f"- {u.text}" for u in sorted(updates, key=lambda u: u.published_at))
        system = _NEUTRALITY + " Be concise."
        user = (
            f"{self._ctx(house_context)}"
            f"Summarize what happened this hour ({hour_label}) in the Big Brother "
            "house as scannable bullets GROUPED BY TOPIC. Format rules:\n"
            "- Each topic gets a short bold header on its own line, e.g. "
            "**Showmance Watch**, **HOH Competition**, **Preseason Buzz** "
            "(2-4 words; only include topics that actually appear).\n"
            "- Under each header, 1-3 short bullets, each starting with '- ' and "
            "kept to a single line.\n"
            "- Leave a blank line between topic groups.\n"
            "- Group related updates together; never repeat the same point under "
            "two headers. One group is fine if that's all there is.\n"
            "- No intro or closing paragraph — start straight at the first header.\n\n"
            f"UPDATES:\n{body}"
        )
        text = await self.llm.text(system, user, max_tokens=700)
        if not text:
            return None
        embed = discord.Embed(
            title=f"House Summary — {hour_label}",
            description=sentence_clamp(strip_links(text), 4000),
            color=0x5865F2, timestamp=datetime.now(self.tz),
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
            description=sentence_clamp(strip_links(text), 4000),
            color=0xFF6B35, timestamp=datetime.now(self.tz),
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
        items = [f"• {self._clean_item(u)}" for u in top]
        embed = discord.Embed(
            title=f"House Summary — {hour_label}",
            description="\n".join(fit_whole_items(items, 3900)), color=0x9B59B6,
            timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"{len(updates)} updates this hour")
        return embed

    def _pattern_whats_happening(self, top: list[Update], total: int) -> discord.Embed:
        items = [f"{i}. {self._clean_item(u)}" for i, u in enumerate(top, 1)]
        embed = discord.Embed(
            title="What's happening right now",
            description="\n".join(fit_whole_items(items, 3900)), color=0xFF6B35,
            timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"Based on {total} updates in the last 24h")
        return embed

    @staticmethod
    def _clean_item(u: Update) -> str:
        """One update as display text: link-free and never cut mid-sentence.
        The 900-char per-item cap only matters for unusually long RSS posts,
        and even then the cut lands on a sentence boundary."""
        return sentence_clamp(strip_links(u.text), 900)

    @staticmethod
    def _trim(text: str, limit: int = 180) -> str:
        """LLM-INPUT budgeting only (e.g. /ask context) — never used for
        anything shown to users."""
        return text if len(text) <= limit else text[: limit - 3] + "..."
