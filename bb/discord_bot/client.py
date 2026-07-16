"""The Discord bot: wires every component together and runs the loops.

Loops:
  * ingest (every 2 min)   -> pull sources, store new, extract (with house
                              context), feed trackers, post urgent moments.
  * hourly (top of hour)   -> post a neutral hour summary built from the DB
                              and STORE it (map step for the daily recap).
  * daily  (06:00 local)   -> reduce the day's stored hourly summaries into a
                              full recap and decay stale alliances.

There is no in-memory queue: summaries are built by querying the DB for a time
window, so a restart never loses or double-posts anything.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from ..analysis.extract import Extractor
from ..analysis.summarize import (Summarizer, event_category, sentence_clamp,
                                  strip_links, urgent_keyword)
from ..config import Season, Settings
from ..db import Database
from ..ingest.bluesky import BlueskySource
from ..ingest.feedstate import (STATE_ANIPALS, STATE_LIVE, STATE_WBRB,
                                FeedStateMonitor, duration_in,
                                duration_minutes, strip_hashtags)
from ..ingest.pipeline import IngestPipeline
from ..ingest.rss import RSSSource
from ..llm import LLM
from ..roster import Roster
from ..trackers.alliances import AllianceTracker
from ..trackers.game_state import GameStateTracker
from ..trackers.relationships import RelationshipTracker
from ..trackers.votes import VoteTracker

log = logging.getLogger("bb.bot")


def _names_in(text: str, names: list[str]) -> list[str]:
    """Roster names appearing in text (word-boundary, case-insensitive), sorted."""
    return sorted(n for n in names
                  if re.search(rf"\b{re.escape(n)}\b", text, re.IGNORECASE))

_ROLE_LABELS = {"hoh": "HOH", "nominee": "Nominees", "veto_winner": "Veto winner",
                "veto_used_on": "Veto used on", "evicted": "Evicted",
                "replacement_nominee": "Replacement nominee"}

# Quiet-hour material. BB tradition: the ants are the house's longest-running
# alliance. Capped at 3 consecutive posts — the third signs off, then the bot
# stays silent until the feeds produce something.
ANT_LINES = [
    "No updates this hour. The ants remain the most active competitors in the house.",
    "Quiet hour on the feeds. The ant alliance, however, has never been stronger — undefeated since BB6.",
    "Nothing to report. The ants just won their 47th consecutive kitchen comp.",
    "All quiet. Somewhere in the storage room, the ants are holding a house meeting.",
    "Feeds are calm. The ants have formed a six-legged voting bloc and refuse to be evicted.",
    "No houseguest activity. The ants, meanwhile, are running laps in the honey jar someone left out.",
    "Nothing happening — unless you count the ants studying the memory wall.",
    "Quiet hour. Production has still not addressed the ants. The ants know this.",
    "Zero updates. The ants have declared themselves Head of Household by squatter's rights.",
    "Feeds are sleepy. The ants are the only ones campaigning right now.",
]
ANT_SIGNOFF = ("Still quiet. Even the ants have gone to bed — I'll pipe back up "
               "when something actually happens.")


class BBBot(commands.Bot):
    BREAKING_COOLDOWN_S = 90 * 60  # per-EVENT 🚨 suppression window

    def __init__(self, settings: Settings, season: Season):
        intents = discord.Intents.default()
        intents.members = True  # needed to pick a random member for /zing
        super().__init__(command_prefix="!bb", intents=intents)

        self.settings = settings
        self.season = season
        self.tz = ZoneInfo(settings.timezone)          # operator/ops clock
        # The Big Brother house is on US/Pacific. Everything the AUDIENCE reads
        # as "house time" — hourly headers, the day a recap covers — uses this
        # fixed clock, independent of where the bot is hosted or TIMEZONE.
        self.house_tz = ZoneInfo("US/Pacific")

        # Components
        self.db = Database(settings.database_url)
        self.llm = LLM(settings.anthropic_api_key, settings.llm_model,
                       settings.llm_rpm, settings.llm_rph,
                       recap_model=settings.llm_model_recap)
        self.roster = Roster.from_season(season)

        self.rss_source = RSSSource(
            season.rss_url,
            fallback_urls=season.rss_fallback_urls,
            proxy_templates=season.rss_proxy_templates,
            poll_interval_s=season.rss_poll_interval_s)
        # Independent second sources. Jokers is the richest feed but its host
        # intermittently refuses datacenter IPs, so a genuinely separate site
        # (different host, different IP) keeps the pipeline fed when it drops —
        # unlike hostname "fallbacks", which all resolved to the same machine.
        extra = [
            RSSSource(f["url"], name=f.get("name", "rss2"),
                      proxy_templates=season.rss_proxy_templates,
                      poll_interval_s=int(f.get("poll_interval_s",
                                                season.rss_poll_interval_s)))
            for f in season.extra_rss_feeds if f.get("url")
        ]
        for e in extra:
            log.info("extra RSS source: %s", e.name)
        sources = [self.rss_source, *extra,
                   BlueskySource(season.bluesky_accounts, self.roster, season.bb_keywords)]
        self.feedstate = FeedStateMonitor(season.feedstate_handle)
        self.pipeline = IngestPipeline(self.db, sources)
        self.extractor = Extractor(self.llm, self.roster)
        self.summarizer = Summarizer(self.llm, self.house_tz, self.roster)
        self.alliances = AllianceTracker(self.db)
        self.relationships = RelationshipTracker(self.db)
        self.game_state = GameStateTracker(self.db, season.start_date,
                                             season.house_day_one)
        self.votes = VoteTracker(self.db)

        self._recent_for_context: list = []  # last few processed updates
        self._breaking_last: dict[str, float] = {}  # event-category -> monotonic ts
        self._breaking_recent: list[str] = []       # lines already alerted (LLM dedupe)
        self._quiet_streak: int = 0
        self._ant_bag: list[str] = []
        self._live_mode: bool = False

    # --- lifecycle ----------------------------------------------------------
    async def setup_hook(self) -> None:
        await self.db.connect()
        await self._merge_runtime_roster()
        from .commands import BBCommands
        from .zings import ZingCog
        await self.add_cog(BBCommands(self))
        await self.add_cog(ZingCog(self))
        synced = await self.tree.sync()
        log.info("synced %d slash commands", len(synced))

        if self.roster.is_empty:
            log.warning("Roster is EMPTY — fill in season.yaml. Extraction stays "
                        "disabled until the roster is populated.")
        self.ingest_loop.start()
        self.hourly_loop.start()
        self.daily_loop.start()
        self.briefing_loop.start()
        if self.season.feedstate_enabled:
            self.feedstate_loop.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%d guilds)", self.user, len(self.guilds))

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    # --- helpers ------------------------------------------------------------
    async def update_channel(self) -> discord.TextChannel | None:
        cid = await self.db.kv_get("update_channel_id") or self.settings.update_channel_id
        if not cid:
            return None
        ch = self.get_channel(int(cid))
        return ch if isinstance(ch, discord.TextChannel) else None

    async def recap_channel(self) -> discord.TextChannel | None:
        """Where the daily/weekly recaps go. Falls back to the main update
        channel when RECAP_CHANNEL_ID (or /setrecapchannel) isn't set, so
        behaviour is unchanged unless you opt in."""
        cid = (await self.db.kv_get("recap_channel_id")
               or self.settings.recap_channel_id)
        if not cid:
            return await self.update_channel()
        ch = self.get_channel(int(cid))
        if isinstance(ch, discord.TextChannel):
            return ch
        log.warning("recap channel %s not found — falling back to updates", cid)
        return await self.update_channel()

    async def feeds_channel(self) -> discord.TextChannel | None:
        """Where feed-state alerts (feeds back / down) go. Falls back to the
        main update channel."""
        cid = (await self.db.kv_get("feeds_channel_id")
               or self.settings.feeds_channel_id)
        if not cid:
            return await self.update_channel()
        ch = self.get_channel(int(cid))
        if isinstance(ch, discord.TextChannel):
            return ch
        log.warning("feeds channel %s not found — falling back", cid)
        return await self.update_channel()

    async def briefing_channel(self) -> discord.TextChannel | None:
        """Where the morning "need to know" brief goes. Falls back to the recap
        channel, then the main update channel."""
        cid = (await self.db.kv_get("briefing_channel_id")
               or self.settings.briefing_channel_id)
        if not cid:
            return await self.recap_channel()
        ch = self.get_channel(int(cid))
        if isinstance(ch, discord.TextChannel):
            return ch
        log.warning("briefing channel %s not found — falling back", cid)
        return await self.recap_channel()

    async def wtf_updates(self, target_hours: int = 8, min_updates: int = 40,
                          max_hours: int = 24):
        """Updates for /wtf, with a window that adapts to house activity.

        A hybrid catch-up/pulse: aim for a tight recent window (default 8h) so a
        busy afternoon reads as "right now", but Big Brother has dead zones —
        the house sleeps, feeds black out for comps — and a fixed short window
        then returns almost nothing. So widen until there are enough updates to
        say something real, capping at max_hours. Returns (updates, window_hours,
        recent_count) — recent_count = updates in the ORIGINAL target window,
        which tells the caller how live things actually are.
        """
        recent = await self.db.recent_updates(target_hours)
        recent_count = len(recent)
        if recent_count >= min_updates:
            return recent, target_hours, recent_count
        # too sparse — widen
        hours = target_hours
        updates = recent
        while hours < max_hours and len(updates) < min_updates:
            hours = min(max_hours, hours * 2)
            updates = await self.db.recent_updates(hours)
        return updates, hours, recent_count

    async def build_briefing(self):
        """Assemble the "need to know" brief from the last 24h + tracked state."""
        now_house = dt.datetime.now(self.house_tz)
        end = now_house.astimezone(dt.timezone.utc)
        start = end - dt.timedelta(hours=24)
        hourlies = await self.db.summaries_between("hourly", start, end)
        alliances = await self.alliances.active()
        rels = await self.relationships.notable()
        return await self.summarizer.need_to_know(
            hourlies, self.game_state.current_day(now_house.date()),
            alliances, rels, await self.house_context())

    async def _merge_runtime_roster(self) -> None:
        """Re-apply roster changes made at runtime (/addhouseguest etc., stored
        in bot_kv) on top of season.yaml so they survive restarts. Order
        matters: adds, then nicknames, then removals (removals win)."""
        try:
            for n in (await self.db.kv_get("roster_extra") or []):
                self.roster.add(n)
            for nick, target in (await self.db.kv_get("nickname_extra") or {}).items():
                self.roster.add_nickname(nick, target)
            for n in (await self.db.kv_get("roster_removed") or []):
                self.roster.remove(n)
            if self.roster.names:
                log.info("roster ready: %d houseguests", len(self.roster.names))
        except Exception as e:
            log.error("runtime roster merge failed: %s", e)

    # --- admin DM nudges ------------------------------------------------------
    async def _send_admin_dm(self, embed: discord.Embed) -> bool:
        """DM the configured owner. Returns False (and logs why) if OWNER_ID is
        unset, the user can't be fetched, or their DMs are closed."""
        if not self.settings.owner_id:
            log.warning("admin nudge skipped: OWNER_ID env var is not set")
            return False
        try:
            user = self.get_user(self.settings.owner_id) or \
                await self.fetch_user(self.settings.owner_id)
            await user.send(embed=embed)
            return True
        except discord.Forbidden:
            log.warning("admin nudge failed: owner's DMs are closed to this bot "
                        "(Privacy Settings -> allow DMs from server members)")
        except Exception as e:
            log.error("admin nudge failed: %s", e)
        return False

    async def _admin_nudges(self, now: dt.datetime) -> None:
        """Once a day, DM the owner a to-do list of human-verification tasks
        the pipeline can't do itself. Each item re-nudges only after its own
        interval (tracked in bot_kv), so nothing spams and nothing is
        forgotten. Items:
          * roster empty with the premiere <= 7 days away (or started)
          * no HOH / nominees recorded this game week when they should exist
          * unlocked alliances at established confidence awaiting
            /confirmalliance / /rejectalliance review
        """
        try:
            items: list[tuple[str, str, int]] = []  # (kv key, message, re-nudge days)

            days_to = (self.season.start_date - now.date()).days
            if self.roster.is_empty and days_to <= 7:
                if days_to > 0:
                    items.append((
                        "roster_empty",
                        f"**Roster is empty** and the premiere is in {days_to} "
                        "day(s). Fill it via `/addhouseguest` (or season.yaml + "
                        "redeploy) — extraction stays disabled until then.", 1))
                else:
                    items.append((
                        "roster_empty",
                        "**The season has started but the roster is EMPTY** — "
                        "extraction is disabled. `/addhouseguest` now.", 1))

            if self._in_season() and not self.roster.is_empty:
                week = self.game_state.current_week()
                days_into = (self.game_state.current_day(now.date()) - 1) % 7
                state = await self.game_state.current(week)
                if days_into >= 1 and not state.get("hoh"):
                    items.append((
                        f"hoh_missing_w{week}",
                        f"**No HOH recorded for week {week}.** If the feeds "
                        f"missed it: `/setgamestate hoh <name>`", 2))
                if days_into >= 2 and not state.get("nominee"):
                    items.append((
                        f"noms_missing_w{week}",
                        f"**No nominees recorded for week {week}.** If missed: "
                        f"`/setgamestate nominee <name>` (once per nominee)", 2))

                pending = [a for a in await self.alliances.active()
                           if not a["locked"] and a["confidence"] >= 0.6]
                if pending:
                    lines = "\n".join(
                        f"  #{a['id']} **{a['name'] or '/'.join(a['members'])}** — "
                        f"{', '.join(a['members'])} ({a['confidence']:.0%})"
                        for a in pending[:6])
                    items.append((
                        "alliance_review",
                        f"**{len(pending)} alliance(s) the bot is tracking on its "
                        f"own:**\n{lines}\n"
                        "_No action needed_ — these are tracked, promoted and "
                        "dissolved automatically as evidence changes. Step in only "
                        "to overrule the bot:\n"
                        "`/alliance <id>` — see the evidence behind one\n"
                        "`/confirmalliance <id>` — you know it's real (pins it, "
                        "immune to decay; can still fracture)\n"
                        "`/rejectalliance <id>` — it's not real (kills it for good)\n"
                        "`/unlockalliance <id>` — hand one back to automatic "
                        "tracking.", 3))

            if not items:
                return
            sent: dict = await self.db.kv_get("admin_nudges") or {}
            due = []
            for key, msg, renudge_days in items:
                last = sent.get(key)
                try:
                    stale = (last is None or
                             (now.date() - dt.date.fromisoformat(last)).days >= renudge_days)
                except (ValueError, TypeError):
                    stale = True
                if stale:
                    due.append((key, msg))
            if not due:
                return
            embed = discord.Embed(
                title="🔔 Admin to-do", color=0xF39C12,
                description="\n\n".join(m for _, m in due)[:4000],
                timestamp=now)
            embed.set_footer(text="Alliance tracking runs automatically — these are FYI, "
                                  "not chores.")
            if await self._send_admin_dm(embed):
                for key, _ in due:
                    sent[key] = now.date().isoformat()
                await self.db.kv_set("admin_nudges", sent)
        except Exception as e:
            log.error("admin nudges failed: %s", e)

    async def _check_feed_stall(self, now: dt.datetime) -> None:
        """In-season dead-feed detector: if nothing has been ingested for 6+
        hours, DM the owner once per day. A stalled ingest usually means the
        Jokers RSS endpoint changed or Bluesky auth broke — silent data loss
        the channel would never surface."""
        try:
            if not self._in_season():
                return
            last = await self.db.fetchval("SELECT max(ingested_at) FROM updates")
            if last is None:
                return
            age_h = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 3600
            if age_h < 6:
                return
            today = now.date().isoformat()
            if await self.db.kv_get("stall_alerted_date") == today:
                return
            await self.db.kv_set("stall_alerted_date", today)
            await self._send_admin_dm(discord.Embed(
                title="⚠️ Feed stall", color=0xE74C3C,
                description=(f"No feed updates ingested in **{age_h:.0f} hours** "
                             "during the season. Jokers RSS or Bluesky auth may be "
                             "down — check the Railway logs."),
                timestamp=now))
        except Exception as e:
            log.error("feed stall check failed: %s", e)

    async def _check_source_health(self, now: dt.datetime) -> None:
        """DM the admin when a SINGLE source is dead but the others still work.
        The existing stall check only fires when NOTHING is arriving, so a
        totally dead RSS feed could (and did) go unnoticed for hours while
        Bluesky trickled along."""
        if not self._in_season():
            return
        fails = getattr(self.rss_source, "consecutive_failures", 0)
        if fails < 15:          # ~30 min of 2-minute polls
            return
        today = now.date().isoformat()
        if await self.db.kv_get("nudge_rss_date") == today:
            return
        embed = discord.Embed(
            title="⚠️ Jokers RSS feed is down",
            description=(f"The RSS source has failed **{fails} polls in a row** "
                         "(~30+ min), including every proxy. Bluesky is likely "
                         "still working, so summaries keep posting — but they're "
                         "missing the richest source of house updates.\n\n"
                         "Jokers blocks datacenter IPs, so the bot normally "
                         "reaches it through a proxy. If that proxy is down or "
                         "rate-limited, add another under `rss_proxy_templates` "
                         "in season.yaml."),
            color=0xE67E22)
        if await self._send_admin_dm(embed):
            await self.db.kv_set("nudge_rss_date", today)

    async def _check_llm_health(self, now: dt.datetime) -> None:
        """DM the admin when summaries have silently degraded to raw lists —
        either no API key at all, or repeated call failures (dead key, quota,
        outage). Once per day, only in season, marker advances only if the
        DM sends."""
        if not self._in_season():
            return
        problem = None
        if not self.llm.available:
            problem = ("LLM is OFF — no `ANTHROPIC_API_KEY` on Railway. "
                       "Summaries and recaps are falling back to raw update lists.")
        elif self.llm.consecutive_failures >= 3:
            problem = (f"LLM calls are failing ({self.llm.consecutive_failures} in a "
                       "row) — check the API key, credit balance, and Railway logs. "
                       "Summaries are falling back to raw update lists.")
        if not problem:
            return
        today = now.date().isoformat()
        if await self.db.kv_get("nudge_llm_date") == today:
            return
        embed = discord.Embed(title="⚠️ Summaries are degraded",
                              description=problem, color=0xE67E22)
        if await self._send_admin_dm(embed):
            await self.db.kv_set("nudge_llm_date", today)

    # --- live-feed state (via @feed-bot.bsky.social) --------------------------
    _FEED_STATE_STYLE = {
        STATE_LIVE:    ("🟢 Feeds are BACK", 0x2ECC71),
        STATE_ANIPALS: ("🐾 Feeds cut to Anipals", 0xF1C40F),
        STATE_WBRB:    ("⏸️ WBRB — feeds are down", 0x95A5A6),
    }

    @tasks.loop(seconds=60)
    async def feedstate_loop(self) -> None:
        try:
            await self._poll_feed_state()
        except Exception as e:
            log.error("feedstate loop error: %s", e)

    @feedstate_loop.before_loop
    async def _before_feedstate(self) -> None:
        await self.wait_until_ready()

    async def _poll_feed_state(self) -> None:
        """Relay the upstream feed-state account.

        These are EVENTS, not state transitions. The original code only
        announced when the state CHANGED — but @feed-bot posts almost nothing
        except "Feeds are back", so the tracked state went live -> live -> live,
        `changed` was never true, and after the first post the bot went
        permanently silent. Every NEW post is now relayed.

        A post must still be FRESH (<15 min) so a restart quietly absorbs
        history instead of replaying yesterday's outages. State is recorded to
        bot_kv regardless, so /feeds stays accurate.
        """
        if not self._in_season():
            return
        sig = await self.feedstate.fetch_signal()
        if sig is None:
            return
        prev = await self.db.kv_get("feed_state") or {}
        if sig["post_url"] and sig["post_url"] == prev.get("post_url"):
            return  # already relayed this exact post
        fresh = (dt.datetime.now(dt.timezone.utc) - sig["created_at"]
                 ) <= dt.timedelta(minutes=15)
        await self.db.kv_set("feed_state", {
            "state": sig["state"],
            "since": sig["created_at"].isoformat(),
            "text": sig["text"][:300],
            "post_url": sig["post_url"],
        })
        if not fresh:
            return

        # Short blips are noise, especially overnight — a 7-minute WBRB isn't
        # news. Suppress "feeds are back" below the threshold; the state is
        # still recorded above, so /feeds stays accurate.
        if sig["state"] == STATE_LIVE:
            mins = duration_minutes(sig["text"])
            floor = self.season.feeds_back_min_minutes
            if mins is not None and mins < floor:
                log.info("suppressing feeds-back relay (%dm outage, floor %dm)",
                         mins, floor)
                return

        channel = await self.feeds_channel()
        if not channel:
            return
        _title, color = self._FEED_STATE_STYLE[sig["state"]]
        # Relay their wording verbatim — the duration is the whole point, and
        # they say it better than a paraphrase would. Hashtags stripped.
        body = strip_hashtags(sig["text"])
        embed = discord.Embed(description=body, color=color,
                              timestamp=sig["created_at"])
        await channel.send(embed=embed)
        log.info("relayed feed-state post: %s", sig["state"])

    async def _feeds_are_live(self) -> bool:
        """False when hard game facts must NOT be written:
          * an admin has paused writes (bot_kv 'live_writes_paused' = true) —
            used pre-premiere when feeds are off and everything is rumor, or
          * the feed-state monitor positively reports feeds down (anipals/WBRB),
            i.e. a comp or ceremony is happening and rumors are flying.
        An UNKNOWN feed state does NOT suppress (the upstream monitor may just
        not be running), so normal tracking is never silently disabled."""
        if await self.db.kv_get("live_writes_paused"):
            return False
        fs = await self.db.kv_get("feed_state") or {}
        if fs.get("state") in (STATE_ANIPALS, STATE_WBRB):
            return False
        return True

    def is_admin(self, interaction: discord.Interaction) -> bool:
        if self.settings.owner_id and interaction.user.id == self.settings.owner_id:
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.administrator)

    def episode_now(self) -> dict | None:
        """The episode window we are currently inside, or None.
        Returned dict has 'live': True for the Thursday live show."""
        now = dt.datetime.now(self.tz)
        minutes = now.hour * 60 + now.minute
        for ep in self.season.episodes:
            if now.weekday() == ep["weekday"] and ep["start_min"] <= minutes < ep["end_min"]:
                return ep
        return None

    EPISODE_RECAP_BUFFER_MIN = 90   # minutes after an episode ends to capture reactions

    @staticmethod
    def _ep_key(day, ep) -> str:
        return f"{day.isoformat()}:{ep['start_min']}"

    def _ep_window(self, day, ep) -> tuple[dt.datetime, dt.datetime]:
        """(start_utc, end_utc+buffer) for episode `ep` occurring on local `day`."""
        start_local = dt.datetime.combine(
            day, dt.time(ep["start_min"] // 60, ep["start_min"] % 60), self.tz)
        end_local = dt.datetime.combine(
            day, dt.time(ep["end_min"] // 60, ep["end_min"] % 60), self.tz)
        end_local += dt.timedelta(minutes=self.EPISODE_RECAP_BUFFER_MIN)
        return start_local.astimezone(dt.timezone.utc), end_local.astimezone(dt.timezone.utc)

    def _recent_episode(self) -> tuple | None:
        """The most recently-FINISHED episode (including buffer): returns
        (key, start_utc, end_utc, label) or None if none in the last 8 days."""
        now = dt.datetime.now(self.tz)
        best = None
        for back in range(0, 8):
            day = now.date() - dt.timedelta(days=back)
            for ep in self.season.episodes:
                if day.weekday() != ep["weekday"]:
                    continue
                start_utc, end_utc = self._ep_window(day, ep)
                if end_utc <= now.astimezone(dt.timezone.utc):
                    label = start_utc.astimezone(self.tz).strftime("%a %b %d")
                    if best is None or start_utc > best[1]:
                        best = (self._ep_key(day, ep), start_utc, end_utc, label)
        return best

    async def _generate_episode_recap(self, start_utc, end_utc, label,
                                      *, force: bool) -> discord.Embed | None:
        updates = await self.db.updates_between(start_utc, end_utc)
        if not updates:
            return None
        context = await self.house_context()
        return await self.summarizer.episode_recap(updates, label, context)

    async def _maybe_post_episode_recap(self) -> None:
        # Auto-posting is OFF by default — the episode recap is long and
        # duplicates the daily recap. /episoderecap still generates one on
        # demand. Flip episode_recap_enabled in season.yaml to bring it back.
        if not self.season.episode_recap_enabled:
            return
        """Auto-post an episode recap once the episode window + buffer has
        elapsed. State in bot_kv (last recapped key) makes it fire exactly once
        and survive restarts; a long outage self-heals (fires late, not never)."""
        if not self._in_season():
            return
        rec = self._recent_episode()
        if not rec:
            return
        key, start_utc, end_utc, label = rec
        last = await self.db.kv_get("last_episode_recap")
        if last is None:
            # First run of this feature: seed the marker instead of back-filling
            # a recap for an already-aired episode. Auto-recaps begin with the
            # next episode; use /episoderecap to get the most recent one now.
            await self.db.kv_set("last_episode_recap", key)
            return
        if last == key:
            return  # already done
        # Only fire once we are actually past the (buffered) end.
        if dt.datetime.now(dt.timezone.utc) < end_utc:
            return
        embed = await self._generate_episode_recap(start_utc, end_utc, label, force=False)
        # Mark done regardless of whether there was content, so an empty episode
        # window doesn't get retried every tick.
        await self.db.kv_set("last_episode_recap", key)
        if not embed:
            return
        channel = await self.update_channel()
        if channel:
            await channel.send(embed=embed)
            log.info("posted episode recap for %s", label)

    def _next_ant_line(self) -> str:
        if not self._ant_bag:
            import random
            self._ant_bag = ANT_LINES[:]
            random.shuffle(self._ant_bag)
        return self._ant_bag.pop()

    def _in_season(self) -> bool:
        """True once the premiere date (from season.yaml) has arrived. Before that
        the bot ingests in the background but posts nothing to the channel."""
        return dt.datetime.now(self.tz).date() >= self.season.start_date

    # breaking category -> the game_state role that proves it already happened
    _BREAKING_ROLE = {"hoh_win": "hoh", "veto_win": "veto_winner",
                      "veto_ceremony": "veto_used_on",
                      "nominations": "nominee", "eviction": "evicted"}

    async def _breaking_is_stale(self, category: str) -> bool:
        role = self._BREAKING_ROLE.get(category)
        if not role:
            return False          # blowups/twists have no game-state record
        state = await self.game_state.current(self.game_state.current_week())
        return bool(state.get(role))

    async def house_context(self) -> str:
        """Short current-state block injected into extraction and summary
        prompts: week, game state, active alliances. Empty pre-roster."""
        if self.roster.is_empty:
            return ""
        parts: list[str] = []
        try:
            week = self.game_state.current_week()
            parts.append(f"Week {week}, Day {self.game_state.current_day()}.")
            state = await self.game_state.current(week)
            for role, names in state.items():
                parts.append(f"{_ROLE_LABELS.get(role, role)}: {', '.join(names)}.")
            rows = await self.alliances.active()
            named = []
            for a in rows[:8]:
                if a["confidence"] >= 0.6 or a["locked"]:
                    label = a["name"] or "/".join(a["members"])
                    named.append(f"{label} ({', '.join(a['members'])})")
            if named:
                parts.append("Active alliances: " + "; ".join(named) + ".")
        except Exception as e:
            log.error("house_context failed: %s", e)
        return " ".join(parts)

    async def recap_context(self) -> str:
        """house_context plus the tracked relationship beats and vote board —
        the richer state a daily/weekly recap should narrate around. Extraction
        keeps using the leaner house_context; only recaps need this."""
        base = await self.house_context()
        if self.roster.is_empty:
            return base
        extras: list[str] = []
        try:
            rel = await self.relationships.notable()
            if rel:
                bits = [f"{r['hg_a']} & {r['hg_b']} ({r['label']})" for r in rel]
                extras.append("Relationship beats: " + "; ".join(bits) + ".")
            counts = await self.votes.current(self.game_state.current_week())
            if counts:
                board = "; ".join(
                    f"{len(v)} to evict {t}" for t, v in
                    sorted(counts.items(), key=lambda kv: len(kv[1]), reverse=True))
                extras.append("Vote board: " + board + ".")
        except Exception as e:
            log.error("recap_context failed: %s", e)
        return (base + " " + " ".join(extras)).strip()

    # --- loops --------------------------------------------------------------
    @tasks.loop(minutes=2)
    async def ingest_loop(self) -> None:
        try:
            episode = self.episode_now()

            # Live-show mode: poll every 30s during the Thursday live window so
            # the Blockbuster result and the eviction land fast.
            want_live = bool(episode and episode["live"] and self._in_season())
            if want_live != self._live_mode:
                self._live_mode = want_live
                if want_live:
                    self.ingest_loop.change_interval(seconds=30)
                    log.info("live-show mode ON: polling every 30s")
                else:
                    self.ingest_loop.change_interval(minutes=2)
                    log.info("live-show mode OFF: polling every 2m")

            await self._maybe_post_episode_recap()

            new_updates = await self.pipeline.run()
            if not new_updates:
                return

            # During a NON-live episode (Wed/Sun), Bluesky posts are episode
            # chatter recapping old footage: never Breaking-worthy and never
            # allowed to write hard facts. RSS (feeds-only) stays trusted.
            recap_airing = bool(episode and not episode["live"])
            hash_src = {u.content_hash: u.source for u in new_updates}

            channel = await self.update_channel()
            if channel and self._in_season():
                for u in new_updates:
                    if recap_airing and u.source == "bluesky":
                        continue
                    # Stage 1 (cheap): keyword NOMINATES a candidate. It does not
                    # decide — houseguests say "veto"/"backdoor" all day long.
                    kw = urgent_keyword(u)
                    if not kw:
                        continue
                    names = _names_in(u.text, self.roster.names)
                    # Bluesky urgency must reference an actual houseguest —
                    # kills "who wins HOH tonight??" speculation triggers.
                    if u.source == "bluesky" and not self.roster.is_empty and not names:
                        continue
                    # Stage 2 (cheap): cooldown BEFORE the LLM, so a burst of
                    # chatter about one event can't run up the bill. Keyed on the
                    # EVENT, not the matched keyword — a single veto win gets
                    # reported as "wins the veto" / "won POV" / "won the Power of
                    # Veto", which used to be three different keys and therefore
                    # three separate alerts.
                    category = event_category(kw)
                    key = f"{category}|{','.join(names)}"
                    now_mono = time.monotonic()
                    if now_mono - self._breaking_last.get(key, float("-inf")) < self.BREAKING_COOLDOWN_S:
                        continue
                    # Stage 2.5: an event whose outcome is ALREADY in tracked
                    # game state isn't breaking. Feeds broke it days ago; the
                    # cooldown has long expired by the time the EPISODE airs and
                    # every updater re-narrates it ("Mallory won the veto!") —
                    # old news to a room of live-feeders. DB-backed, so it
                    # survives restarts, and a new week resets it naturally.
                    if await self._breaking_is_stale(category):
                        continue
                    # Stage 3 (LLM): the real gate. Returns None for discussion,
                    # planning, jokes and speculation — and now also for an event
                    # already alerted (different updaters, different wording, same
                    # event), which the category key alone can miss when one post
                    # names extra houseguests.
                    line = await self.summarizer.breaking(
                        u, await self.house_context(),
                        recent_alerts=self._breaking_recent)
                    if not line:
                        continue
                    self._breaking_last[key] = now_mono
                    self._breaking_recent.append(line)
                    del self._breaking_recent[:-8]   # keep the last few only
                    await channel.send(embed=discord.Embed(
                        title="🚨 Breaking", description=line,
                        color=0xE74C3C, timestamp=dt.datetime.now(self.tz)))

            if self.llm.available and not self.roster.is_empty:
                context = await self.house_context()
                extraction = await self.extractor.extract(
                    new_updates,
                    context_updates=self._recent_for_context[-10:],
                    house_context=context,
                    episode_airing=recap_airing,
                )
                if recap_airing:
                    extraction.game_events = [
                        e for e in extraction.game_events
                        if hash_src.get(e.source_hash) != "bluesky"]
                    extraction.vote_plans = [
                        v for v in extraction.vote_plans
                        if hash_src.get(v.source_hash) != "bluesky"]
                await self.alliances.ingest(extraction.alliances)
                await self.relationships.ingest(extraction.relationships)
                # Hard facts (HOH/noms/eviction, vote plans) require a LIVE feed.
                # While feeds are off (pre-premiere) or down (comp/ceremony
                # anipals+WBRB), the wild is all TV announcements and rumor —
                # e.g. "Dee won HOH" before it airs — so game state and votes
                # are not written. Alliances/relationships still track.
                if await self._feeds_are_live():
                    await self.game_state.ingest(extraction.game_events)
                    # An eviction is ground truth for the alliance map too:
                    # the houseguest leaves every alliance, and any group left
                    # below two live members (an orphaned F2) dissolves.
                    for ev in extraction.game_events:
                        if ev.role == "evicted":
                            await self.alliances.handle_eviction(ev.houseguest)
                    await self.votes.ingest(extraction.vote_plans,
                                            self.game_state.current_week())
                elif extraction.game_events or extraction.vote_plans:
                    log.info("feeds not live — held %d game events, %d vote plans",
                             len(extraction.game_events), len(extraction.vote_plans))

            # Keep a small rolling window for the next extraction's context.
            self._recent_for_context = (self._recent_for_context + new_updates)[-10:]
        except Exception as e:
            log.error("ingest loop error: %s", e)

    @tasks.loop(minutes=60)
    async def hourly_loop(self) -> None:
        """Summarize every hour boundary since the last one we processed
        (persisted in bot_kv). Hours missed while the bot was down are
        summarized and STORED so the daily map-reduce has no holes; only the
        current hour is posted to the channel. Catch-up is capped at 24
        windows so a long outage can't flood the LLM."""
        try:
            now = dt.datetime.now(self.house_tz)
            hour_end = now.replace(minute=0, second=0, microsecond=0)

            windows: list[tuple[dt.datetime, dt.datetime]] = []
            last_iso = await self.db.kv_get("last_hourly_end")
            last_end: dt.datetime | None = None
            if isinstance(last_iso, str):
                try:
                    last_end = dt.datetime.fromisoformat(last_iso).astimezone(self.house_tz)
                except ValueError:
                    last_end = None
            if last_end is None or last_end >= hour_end:
                windows.append((hour_end - dt.timedelta(hours=1), hour_end))
            else:
                cur = max(last_end, hour_end - dt.timedelta(hours=24))
                while cur < hour_end:
                    windows.append((cur, cur + dt.timedelta(hours=1)))
                    cur += dt.timedelta(hours=1)

            for w_start, w_end in windows:
                await self._summarize_hour(w_start, w_end, post=(w_end == hour_end))
            await self.db.kv_set("last_hourly_end", hour_end.isoformat())
            await self._check_llm_health(now)
            await self._check_source_health(now)

            await self._check_feed_stall(now)
        except Exception as e:
            log.error("hourly loop error: %s", e)

    async def _summarize_hour(self, hour_start: dt.datetime,
                              hour_end: dt.datetime, post: bool) -> None:
        start_utc = hour_start.astimezone(dt.timezone.utc)
        end_utc = hour_end.astimezone(dt.timezone.utc)

        # Idempotence: a retried catch-up batch (or a drift-induced re-fire)
        # must not store or post the same window twice.
        if await self.db.fetchval(
                "SELECT 1 FROM summaries WHERE kind = 'hourly' "
                "AND period_start = $1 AND period_end = $2 LIMIT 1",
                start_utc, end_utc):
            return

        updates = await self.db.updates_between(start_utc, end_utc)
        if not updates:
            if not post:
                return  # missed window with nothing in it: nothing to store
            # Quiet hour: the ants take over — but only for 3 in a row,
            # then silence until the feeds wake back up.
            self._quiet_streak += 1
            if self._in_season() and self._quiet_streak <= 3:
                channel = await self.update_channel()
                if channel:
                    line = ANT_SIGNOFF if self._quiet_streak == 3 else self._next_ant_line()
                    await channel.send(embed=discord.Embed(
                        title=f"🐜 House Summary — {hour_end.strftime('%I %p').lstrip('0')}",
                        description=line, color=0x95A5A6,
                        timestamp=dt.datetime.now(self.tz)))
            return
        self._quiet_streak = 0

        context = await self.house_context()
        label = hour_end.strftime("%I %p").lstrip("0")
        embeds = await self.summarizer.hourly(updates, label, context)

        # Map step: store the digest so the daily recap covers the whole
        # day instead of a lossy top-5 of raw updates.
        if embeds and embeds[0].description:
            await self.db.add_summary("hourly", start_utc, end_utc,
                                      embeds[0].description, len(updates))

        if not post or not self._in_season():
            return  # catch-up / off-season: store silently, post nothing
        channel = await self.update_channel()
        if not channel:
            return
        for embed in embeds:
            await channel.send(embed=embed)

    BRIEFING_HOUR = 9    # house time (US/Pacific)

    @tasks.loop(minutes=15)
    async def briefing_loop(self) -> None:
        """Posts the "need to know" brief on the first tick at/after
        BRIEFING_HOUR house time. Marker in bot_kv, so restarts can't
        double-post or silently skip a day."""
        try:
            now_house = dt.datetime.now(self.house_tz)
            if now_house.hour < self.BRIEFING_HOUR:
                return
            today = now_house.date().isoformat()
            if await self.db.kv_get("last_briefing_date") == today:
                return
            await self.db.kv_set("last_briefing_date", today)
            if not self._in_season():
                return
            embed = await self.build_briefing()
            if not embed:
                return
            channel = await self.briefing_channel()
            if channel:
                await channel.send(embed=embed)
                log.info("posted need-to-know briefing")
        except Exception as e:
            log.error("briefing loop error: %s", e)

    @briefing_loop.before_loop
    async def _before_briefing(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=15)
    async def daily_loop(self) -> None:
        """Fires on the first tick at/after 07:00 Pacific (Big Brother house
        time) and recaps the PREVIOUS house day. Markers live in bot_kv, so a
        restart can neither double-post a recap nor permanently skip a day the
        bot was down for at 7am."""
        try:
            now_house = dt.datetime.now(self.house_tz)
            if now_house.hour < 7:
                return
            today_house = now_house.date()
            if await self.db.kv_get("last_daily_date") == today_house.isoformat():
                return
            await self.db.kv_set("last_daily_date", today_house.isoformat())

            await self._admin_nudges(now_house)

            if not self._in_season():
                return  # off-season: no daily recap until the season starts

            dissolved = await self.alliances.decay()
            if dissolved:
                log.info("daily decay dissolved %d alliances", dissolved)
            await self.relationships.decay()

            channel = await self.recap_channel()
            if not channel:
                return

            # Recap the previous house day: midnight -> midnight Pacific of
            # yesterday, titled with THAT day's house-day number (e.g. a 7am
            # recap on Day 2 covers and is titled "Day 1").
            yesterday_house = today_house - dt.timedelta(days=1)
            day_number = self.game_state.current_day(yesterday_house)
            start_utc = dt.datetime.combine(
                yesterday_house, dt.time.min, self.house_tz).astimezone(dt.timezone.utc)
            end_utc = dt.datetime.combine(
                today_house, dt.time.min, self.house_tz).astimezone(dt.timezone.utc)
            hourlies = await self.db.summaries_between("hourly", start_utc, end_utc)
            fallback = await self.db.recent_updates(24)
            context = await self.recap_context()
            embed = await self.summarizer.daily_recap(
                hourlies, fallback, day_number, context)
            await channel.send(embed=embed)
            if embed.description:
                await self.db.add_summary("daily", start_utc, end_utc,
                                          embed.description,
                                          sum(h["update_count"] for h in hourlies) or len(fallback))

            # Weekly recap: post the most recently completed game week if it
            # hasn't been posted yet. No day-modulo gate — if the bot is down
            # on the boundary morning, the recap self-heals the next morning
            # instead of being dropped. Window is computed from the week
            # number (same math as /week), not "last 7 days from now".
            completed = (today_house - self.season.start_date).days // 7
            last_weekly = int(await self.db.kv_get("last_weekly") or 0)
            if completed >= 1 and completed > last_weekly:
                await self.db.kv_set("last_weekly", completed)
                wk_start_date = self.season.start_date + dt.timedelta(days=7 * (completed - 1))
                wk_end_date = wk_start_date + dt.timedelta(days=7)
                wk_start = dt.datetime.combine(
                    wk_start_date, dt.time.min, self.house_tz).astimezone(dt.timezone.utc)
                wk_end = dt.datetime.combine(
                    wk_end_date, dt.time.max, self.house_tz).astimezone(dt.timezone.utc)
                dailies = await self.db.summaries_between("daily", wk_start, wk_end)
                wembed = await self.summarizer.weekly_recap(dailies, completed, context)
                await channel.send(embed=wembed)
        except Exception as e:
            log.error("daily loop error: %s", e)

    @ingest_loop.before_loop
    async def _before_ingest(self) -> None:
        await self.wait_until_ready()

    @daily_loop.before_loop
    async def _before_daily(self) -> None:
        await self.wait_until_ready()

    @hourly_loop.before_loop
    async def _before_hourly(self) -> None:
        await self.wait_until_ready()
        now = dt.datetime.now(self.tz)
        nxt = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        await asyncio.sleep((nxt - now).total_seconds())
