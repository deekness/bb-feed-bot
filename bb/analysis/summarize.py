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

# Candidate triggers ONLY — a keyword match merely sends the update to the LLM
# gate below, which decides if it is a real, COMPLETED event. Strategy words
# ("backdoor", "target", "blindside") are deliberately NOT here: houseguests
# discuss them constantly, and discussion is not news.
URGENT_KEYWORDS = (
    # comp results
    "wins hoh", "won hoh", "wins the hoh", "is the new hoh", "new hoh",
    "wins head of household", "won head of household",
    "wins veto", "won veto", "wins the veto", "won the veto", "wins pov",
    "won pov", "wins the pov", "won the power of veto", "wins the power of veto",
    "won the golden power of veto", "wins the golden power of veto",
    # ceremony outcomes
    "nomination ceremony", "veto ceremony", "veto meeting", "has been nominated",
    "nominated for eviction", "final nominees", "veto was used", "used the veto",
    "did not use the veto", "vetoed", "replacement nominee",
    # exits
    "evicted", "eviction results", "has been evicted", "self-evict",
    "self-evicted", "expelled", "ejected", "walked out", "quit the game",
    "removed from the house", "removed from the game",
    # rare big twists
    "battle back", "returns to the house", "re-enters", "double eviction",
    "triple eviction", "diamond veto", "coup",
    # blowups
    "screaming match", "blowup", "blow up", "shouting match", "fight broke out",
    "got into it", "yelling at", "screaming at", "in tears", "stormed off",
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


def one_sided_note(a: dict) -> str:
    """Who is playing whom. 'one-sided' alone tells you the deal is fake but
    not the interesting half — which member is doing the faking."""
    by = list(a.get("one_sided_by") or [])
    if not by:
        return "one-sided" if a.get("one_sided") else ""
    members = list(a.get("members") or [])
    played = [m for m in members if m not in by]
    if played:
        return f"{'/'.join(by)} playing {'/'.join(played)}"
    return f"{'/'.join(by)} isn't real on it"


def _wtf_footer(total: int, window_hours: int) -> str:
    """Honest footer: reflect the window actually used, not a hardcoded 24h."""
    if window_hours >= 24:
        span = "24h"
    elif window_hours == 1:
        span = "hour"
    else:
        span = f"{window_hours}h"
    return f"Based on {total} updates in the last {span}"


def strip_links(text: str) -> str:
    """Remove markdown links (keeping their label) and bare URLs. Bot outputs
    are link-free by policy; raw update texts can carry URLs and the LLM will
    happily echo them, so LLM output is scrubbed too."""
    text = _MD_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def drop_orphan_tail(text: str) -> str:
    """Remove a trailing fragment the MODEL left unfinished (token limit hit
    mid-write), e.g. a dangling '- Rome and Lala' or a lone '- Y'.

    Only the FINAL line is ever considered, and only when it is clearly a
    truncation: short and unpunctuated, or a header left with nothing under it.

    The old version looped, popping every bullet that didn't end in a period —
    which is most bullets — and so deleted entire hourly digests, posting a
    blank embed over 70 real updates. A cleaner must never be able to empty its
    own input: if the result would be blank, the original is returned.
    """
    original = text
    lines = text.rstrip().split("\n")

    # 1) trailing blank lines
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return original

    def is_header(b: str) -> bool:
        return b.startswith("**") and b.endswith("**")

    last = lines[-1].strip()
    body = last.lstrip("-•* ").strip()
    is_bullet = last.startswith(("-", "•", "*"))
    finished = body.endswith((".", "!", "?", '"', "”", ")", "*"))

    # 2) a short, unpunctuated final bullet is a cut-off fragment.
    #    A LONG unpunctuated bullet is just a normal bullet — leave it alone.
    if is_bullet and not finished and len(body) < 25:
        lines.pop()

    # 3) a header dangling at the very end with no content under it
    while lines and is_header(lines[-1].strip()):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()

    out = "\n".join(lines).rstrip()
    return out if out.strip() else original

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


_URGENT_RES = tuple(
    (k, re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE)) for k in URGENT_KEYWORDS
)


# A single real event gets reported by many updaters in many phrasings — "wins
# the veto", "won POV", "has won the Power of Veto". Keying the cooldown on the
# matched keyword STRING therefore let one veto win fire three separate alerts.
# Collapse every keyword into the EVENT it describes instead.
_EVENT_CATEGORY = (
    ("veto_win",     ("wins veto", "won veto", "wins the veto", "won the veto",
                      "wins pov", "won pov", "wins the pov",
                      "won the power of veto", "wins the power of veto",
                      "won the golden power of veto",
                      "wins the golden power of veto")),
    ("hoh_win",      ("wins hoh", "won hoh", "wins the hoh", "is the new hoh",
                      "new hoh", "wins head of household",
                      "won head of household")),
    ("veto_ceremony",("veto ceremony", "veto meeting", "veto was used",
                      "used the veto", "did not use the veto", "vetoed",
                      "replacement nominee")),
    ("nominations",  ("nomination ceremony", "has been nominated",
                      "nominated for eviction", "final nominees")),
    ("eviction",     ("evicted", "eviction results", "has been evicted")),
    ("exit",         ("self-evict", "self-evicted", "expelled", "ejected",
                      "walked out", "quit the game", "removed from the house",
                      "removed from the game")),
    ("twist",        ("battle back", "returns to the house", "re-enters",
                      "double eviction", "triple eviction", "diamond veto",
                      "coup")),
    ("blowup",       ("screaming match", "blowup", "blow up", "shouting match",
                      "fight broke out", "got into it", "yelling at",
                      "screaming at", "in tears", "stormed off")),
)


def event_category(keyword: str) -> str:
    """The event a trigger keyword describes — the cooldown key, so that every
    phrasing of one real event collapses to a single alert."""
    for cat, words in _EVENT_CATEGORY:
        if keyword in words:
            return cat
    return keyword


def urgent_keyword(update: Update) -> str | None:
    """The first urgent keyword present in the update, or None. Word-boundary
    matched — a plain substring test made "coup" fire inside "couple" and lit up
    Breaking on ordinary chatter. The keyword only NOMINATES a candidate; the
    LLM gate in Summarizer.breaking() makes the real call.

    Also used as the cooldown key so one real event reported by several sources
    fires a single Breaking post.
    """
    text = update.text
    for k, rx in _URGENT_RES:
        if rx.search(text):
            return k
    return None


def is_urgent(update: Update) -> bool:
    return urgent_keyword(update) is not None


class Summarizer:
    def __init__(self, llm: LLM, tz, roster=None, episode_window=None):
        self.llm = llm
        self.tz = tz
        # Callable(datetime) -> bool: is this timestamp inside a TV episode
        # airing? Updaters live-tweet the broadcast — an EDITED REPLAY of
        # events from earlier days — which must not read as new feed events.
        self.episode_window = episode_window
        # The roster lets prompts state the canonical cast + aliases. Updaters
        # write "Rick" and "Devens" (and "Lala"/"LaTrice") interchangeably —
        # without this the model reports one person as two.
        self.roster = roster

    def _naming_rule(self) -> str:
        if not self.roster or self.roster.is_empty:
            return ""
        cast = ", ".join(sorted(self.roster.names))
        rule = (f"The houseguests are: {cast}. Refer to each ONLY by these exact "
                "names. Never invent houseguests.")
        nicks = self.roster.nicknames
        if nicks:
            pairs = "; ".join(f"'{k}' = {v}" for k, v in sorted(nicks.items()))
            rule += (f" These are aliases for the SAME person, not different people: "
                     f"{pairs}. Always use the canonical name and never treat an "
                     "alias and its canonical name as two separate houseguests.")
        return rule + " "

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
                              house_context: str = "",
                              window_hours: int = 24,
                              recent_count: int | None = None) -> discord.Embed:
        if not updates:
            return discord.Embed(
                title="Nothing's happening",
                description="No updates in the last 24 hours.",
                color=0x95A5A6,
            )
        top = sorted(updates, key=importance, reverse=True)[:15]
        if self.llm.available:
            embed = await self._llm_whats_happening(
                top, len(updates), house_context, window_hours)
            if embed:
                return embed
        return self._pattern_whats_happening(top[:5], len(updates), window_hours)

    # --- breaking alert (LLM-gated; discussion must NOT fire) ---------------
    async def breaking(self, update: Update, house_context: str = "",
                       recent_alerts: list[str] | None = None) -> str | None:
        """Decide whether an update is REALLY breaking, and if so write the
        one-line alert. Returns None to suppress.

        The keyword list only nominates candidates; this is the actual gate.
        Houseguests talk about the veto, backdoors and targets constantly —
        that is conversation, not news. Only a COMPLETED, confirmed event
        (comp result, ceremony outcome, exit, major blowup) is breaking.
        """
        if not self.llm.available:
            return None  # no LLM: stay silent rather than dump raw updater text
        system = (
            _NEUTRALITY +
            " You are triaging a Big Brother live-feed update for a BREAKING "
            "alert. Reply with EXACTLY one of:\n"
            "SKIP — if it is houseguests DISCUSSING, planning, speculating about, "
            "or reacting to something; a joke; general chatter; anything not yet "
            "decided; OR if it reports the SAME EVENT as one of the ALREADY "
            "ALERTED items below, even in different words. Discussion of a "
            "veto/backdoor/target is NOT breaking.\n"
            "ALERT: <one clean sentence> — ONLY if a real event has ACTUALLY "
            "HAPPENED: a competition was won, a ceremony concluded, someone was "
            "nominated/evicted/removed/walked, a major twist occurred, or a "
            "serious fight/blowup broke out.\n"
            "The sentence must be plain, factual, and self-contained. Do not "
            "copy the updater's shorthand, timestamps, or tags like (NT)."
        )
        already = ""
        if recent_alerts:
            joined = "\n".join(f"- {a}" for a in recent_alerts[-6:])
            already = ("ALREADY ALERTED (do not repeat these events):\n"
                       f"{joined}\n\n")
        user = f"{self._ctx(house_context)}{already}UPDATE:\n{update.text}"
        text = await self.llm.text(system, user, max_tokens=120)
        if not text:
            return None
        text = strip_links(text).strip()
        if not text.upper().startswith("ALERT"):
            return None
        line = text.split(":", 1)[1].strip() if ":" in text else ""
        return sentence_clamp(line, 400) or None

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
        system = _NEUTRALITY + " " + self._naming_rule() + "Be concise."
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
            description=sentence_clamp(drop_orphan_tail(strip_links(text)), 4000),
            color=0xF1C40F, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"{len(updates)} updates during & after the episode")
        return embed

    # --- morning briefing: state of the house, not a chronology --------------
    async def need_to_know(self, hourly_summaries: list[dict], day_number: int,
                           alliances: list[dict], relationships: list[dict],
                           house_context: str = "") -> discord.Embed | None:
        """A "what you need to know" brief: where the game STANDS right now,
        rather than what happened in order (that's the daily recap's job).

        Deliberately hybrid. The numbered list is written by the model from the
        last day's summaries, but the alliance map and relationship beats are
        rendered DIRECTLY from the trackers — they're facts the bot already
        holds, so there's nothing for the model to get wrong. That tracked map
        is the thing a human writing this by hand can't easily reproduce.
        """
        if not self.llm.available or not hourly_summaries:
            return None

        blocks = []
        for s_ in hourly_summaries:
            label = s_["period_end"].astimezone(self.tz).strftime("%I %p").lstrip("0")
            blocks.append(f"[{label}] {s_['content']}")
        body = "\n\n".join(blocks)

        system = _NEUTRALITY + " " + self._naming_rule()
        user = (
            f"{self._ctx(house_context)}"
            "Below are the last day's hourly summaries from the Big Brother "
            "house. Write the numbered 'things you need to know' list that a "
            "feed-watcher would want before catching up today.\n\n"
            "RULES:\n"
            "- 5-8 numbered items, most important FIRST (not chronological).\n"
            "- Each item is ONE punchy line stating where things STAND — the "
            "current situation, targets, plans and shifts — not a play-by-play "
            "of what happened when.\n"
            "- Prefer state over story: 'Ashley is the renom plan', 'Yash is now "
            "an early target', 'Mallory is realising Melody isn't in her "
            "corner'.\n"
            "- Mark anything unconfirmed as a plan or a read, never as fact.\n"
            "- Do NOT list alliances — those are added separately below.\n"
            "- No heading, no intro, no closing. Output ONLY the numbered lines."
            f"\n\nSUMMARIES:\n\n{body}"
        )
        text = await self.llm.text(system, user, max_tokens=900, heavy=True)
        if not text:
            return None

        parts = [sentence_clamp(drop_orphan_tail(strip_links(text)), 2200)]

        # Alliance map — straight from the tracker, no LLM in the loop.
        if alliances:
            lines = []
            for a in alliances[:8]:
                members = "/".join(a["members"])
                name = f"**{a['name']}** ({members})" if a.get("name") else f"**{members}**"
                tags = []
                if len(a["members"]) == 2:
                    tags.append("duo")
                note = one_sided_note(a)
                if note:
                    tags.append(f"⚠️ {note}")
                if a.get("status") == "fracturing":
                    tags.append("fracturing")
                suffix = f" — _{', '.join(tags)}_" if tags else ""
                lines.append(f"- {name}{suffix} · {a['confidence']:.0%}")
            parts.append("**The alliance map**\n" + "\n".join(lines))

        # Relationship beats — also straight from the tracker.
        if relationships:
            beats = []
            for r in relationships[:6]:
                label = r.get("label") or ""
                if not label:
                    continue
                beats.append(f"- {r['hg_a']} & {r['hg_b']} — {label}")
            if beats:
                parts.append("**Where they stand**\n" + "\n".join(beats))

        embed = discord.Embed(
            title=f"Need to know — Day {day_number}",
            description=sentence_clamp("\n\n".join(parts), 4000),
            color=0x1D9E75, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text="Where the game stands right now")
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

        system = _NEUTRALITY + " " + self._naming_rule()
        user = (
            f"{self._ctx(house_context)}"
            "Below are the hour-by-hour summaries for the last day in the Big "
            "Brother house. Write a day recap that someone will actually want to "
            "read — scannable, not a wall of text.\n\n"
            "EXACT STRUCTURE (follow it precisely):\n"
            "1. **A one-line hook** in bold — the single sentence that captures "
            "the day. No heading, no title (the post already has one).\n"
            "2. **The Story** — the day's narrative, broken into 2-3 SHORT "
            "paragraphs of 2-4 sentences each. Each paragraph covers ONE thread "
            "(e.g. the comp and its fallout; the alliance shifts; the "
            "showmance/personal drama). Put a blank line between them. Never "
            "write a paragraph longer than 4 sentences — if a thread needs more, "
            "split it. Use the CURRENT HOUSE STATE to explain WHY the day "
            "mattered (who gained power, who got betrayed), not just what "
            "happened.\n"
            "3. **Key Developments** — 4-7 bullets, chronological, each starting "
            "with a bolded 2-4 word label followed by a colon.\n\n"
            "Cover the whole day — do not drop threads that only appear in one "
            "hour. EPISODE RULE: anything the summaries attribute to the TV "
            "episode/broadcast (DR confessionals, comp footage, montages, 'the "
            "episode showed...') is an edited replay of PAST days — never "
            "present it as a new event from this day. Live-feed events that "
            "merely happened while an episode aired are real; feeds stay live "
            "except during Thursday's live show, whose only new facts are a "
            "Block Buster result or an eviction.\n\n"
            f"HOURLY SUMMARIES:\n\n{body}"
        )
        text = await self.llm.text(system, user, max_tokens=2500, heavy=True)
        if not text:
            embed = await self.whats_happening(fallback_updates, house_context)
            embed.title = f"Day {day_number} Recap"
            return embed
        embed = discord.Embed(
            title=f"Day {day_number} Recap",
            description=sentence_clamp(drop_orphan_tail(strip_links(text)), 4000), color=0xFF6B35, timestamp=datetime.now(self.tz),
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
            "Answer using ONLY the material above — verify it against the "
            "updates before answering, but do NOT narrate that process.\n"
            "STYLE — give the answer, not the search:\n"
            "- Lead with the direct answer in the first sentence. No preamble.\n"
            "- Never cite where it came from: no 'per the feed update', no "
            "'according to', no update timestamps, no 'the archive shows'. "
            "Mention a time ONLY when the timing is itself the answer.\n"
            "- Don't editorialize about the sources: no corrections, edits, "
            "post counts, or 'nothing further has surfaced yet'.\n"
            "- Be brief: 1-3 sentences, or a short list when the answer is "
            "several names.\n"
            "- If the answer genuinely isn't in the material, say so in one "
            "short sentence — don't pad it. If something is unconfirmed, say "
            "so in a few words, not a paragraph.\n"
            "Stay neutral toward every houseguest.")

        text = await self.llm.text(_NEUTRALITY, "\n\n".join(parts),
                                   max_tokens=500)
        embed = discord.Embed(
            title=f"❓ {question[:230]}",
            description=(sentence_clamp(drop_orphan_tail(strip_links(text)), 4000)
                         if text else "Couldn't produce an answer — try rewording."),
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
            "house. Write the week's story — scannable, never a wall of text.\n"
            "EXACT STRUCTURE:\n"
            "1. **A one-line hook** in bold — the sentence that captures the week.\n"
            "2. 'The Arc' — the week's story in 3-4 SHORT paragraphs of 2-4 "
            "sentences each, one thread per paragraph (the HOH and her plan; the "
            "veto and its fallout; the social/alliance shifts; the vote). Blank "
            "line between paragraphs. NEVER write a paragraph longer than 4 "
            "sentences — split it. Use the CURRENT HOUSE STATE to explain the "
            "power shifts, not just the sequence of comps.\n"
            "3. 5-8 bullets of key developments, chronological, each starting "
            "with a bolded 2-4 word label and a colon.\n"
            "4. One short 'going into next week' paragraph. GAME RULE: the "
            "outgoing HOH cannot compete in the next HOH — never describe them "
            "as holding power going forward.\n\nDAILY RECAPS:\n\n{blocks}"
        ).replace("{blocks}", blocks)
        text = await self.llm.text(_NEUTRALITY, user, max_tokens=2500, heavy=True)
        embed = discord.Embed(
            title=f"📆 Week {week_number} Recap",
            description=(sentence_clamp(drop_orphan_tail(strip_links(text)), 4000)
                         if text else "Recap generation failed."),
            color=0x8E44AD, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"Built from {len(dailies)} daily recaps")
        return embed

    # --- LLM paths ----------------------------------------------------------
    async def _llm_digest(self, updates: list[Update], hour_label: str,
                          house_context: str) -> discord.Embed | None:
        def line(u):
            # Feeds stay LIVE during Sun/Wed airings — only the Thursday live
            # show takes them down — so a time window alone would smear real
            # feed events. Jokers' timestamped lines are feed reports by
            # construction; it's the social accounts that live-tweet the
            # broadcast. So: tag only bluesky updates inside an air window,
            # and let the model judge the content.
            tag = ("[DURING EPISODE AIRING] "
                   if (u.source == "bluesky" and self.episode_window
                       and self.episode_window(u.published_at))
                   else "")
            return f"- {tag}{u.text}"
        body = "\n".join(line(u) for u in sorted(updates, key=lambda u: u.published_at))
        system = _NEUTRALITY + " " + self._naming_rule() + "Be concise."
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
            "- No intro or closing paragraph — start straight at the first header.\n"
            "- Lines tagged [DURING EPISODE AIRING] were posted while a TV "
            "episode aired. Feeds stay live during Sunday/Wednesday episodes, so "
            "such a line is EITHER narration of the broadcast (comp footage, "
            "Diary Room bits, reaction montages — an edited replay of EARLIER "
            "days) OR a genuine live-feed report. Judge by content: broadcast "
            "narration must never be reported as happening this hour — at most "
            "one bullet under **Episode Watch** noting what the episode covered. "
            "The ONLY new facts a live Thursday episode produces are the Block "
            "Buster result and the eviction result; the first half of live "
            "episodes re-covers events feed-watchers already know.\n\n"
            f"UPDATES:\n{body}"
        )
        text = await self.llm.text(system, user, max_tokens=1600)
        if not text:
            return None
        # Belt and braces: if the cleaners somehow reduce real model output to
        # nothing, fall back to the raw text rather than posting a blank embed
        # over a busy hour. (A bug in drop_orphan_tail once did exactly that.)
        desc = sentence_clamp(drop_orphan_tail(strip_links(text)), 4000)
        if not desc.strip():
            log.warning("hourly digest cleaned to empty — using raw model text")
            desc = strip_links(text).strip()[:4000]
        if not desc.strip():
            return None
        embed = discord.Embed(
            title=f"House Summary — {hour_label}",
            description=desc,
            color=0x5865F2, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=f"{len(updates)} updates this hour")
        return embed

    async def _llm_whats_happening(self, top: list[Update], total: int,
                                   house_context: str,
                                   window_hours: int = 24) -> discord.Embed | None:
        body = "\n".join(f"- {u.text}" for u in top)
        system = (_NEUTRALITY + " Someone is checking in on the house — maybe "
                  "catching up after a few hours away, maybe just seeing what's "
                  "live right now. Serve both.")
        user = (
            f"{self._ctx(house_context)}"
            "From these updates (newest first), give the 5 most important things "
            "as short bullets (one sentence each), then a one-line summary.\n"
            "Lead with what's happening NOW or still unresolved; fold older "
            "threads in only if they're still live. Favour the most recent "
            "developments over things that have already settled.\n\n"
            f"UPDATES:\n\n{body}"
        )
        text = await self.llm.text(system, user, max_tokens=1200)
        if not text:
            return None
        embed = discord.Embed(
            title="What's happening right now",
            description=sentence_clamp(drop_orphan_tail(strip_links(text)), 4000),
            color=0xFF6B35, timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=_wtf_footer(total, window_hours))
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

    def _pattern_whats_happening(self, top: list[Update], total: int,
                                 window_hours: int = 24) -> discord.Embed:
        items = [f"{i}. {self._clean_item(u)}" for i, u in enumerate(top, 1)]
        embed = discord.Embed(
            title="What's happening right now",
            description="\n".join(fit_whole_items(items, 3900)), color=0xFF6B35,
            timestamp=datetime.now(self.tz),
        )
        embed.set_footer(text=_wtf_footer(total, window_hours))
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
