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
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from ..analysis.extract import Extractor
from ..analysis.summarize import Summarizer, is_urgent
from ..config import Season, Settings
from ..db import Database
from ..ingest.bluesky import BlueskySource
from ..ingest.pipeline import IngestPipeline
from ..ingest.rss import RSSSource
from ..llm import LLM
from ..roster import Roster
from ..trackers.alliances import AllianceTracker
from ..trackers.game_state import GameStateTracker
from ..trackers.relationships import RelationshipTracker

log = logging.getLogger("bb.bot")

_ROLE_LABELS = {"hoh": "HOH", "nominee": "Nominees", "veto_winner": "Veto winner",
                "veto_used_on": "Veto used on", "evicted": "Evicted",
                "replacement_nominee": "Replacement nominee"}


class BBBot(commands.Bot):
    def __init__(self, settings: Settings, season: Season):
        intents = discord.Intents.default()
        intents.members = True  # needed to pick a random member for /zing
        super().__init__(command_prefix="!bb", intents=intents)

        self.settings = settings
        self.season = season
        self.tz = ZoneInfo(settings.timezone)

        # Components
        self.db = Database(settings.database_url)
        self.llm = LLM(settings.anthropic_api_key, settings.llm_model,
                       settings.llm_rpm, settings.llm_rph)
        self.roster = Roster.from_season(season)

        sources = [RSSSource(season.rss_url),
                   BlueskySource(season.bluesky_accounts, self.roster, season.bb_keywords)]
        self.pipeline = IngestPipeline(self.db, sources)
        self.extractor = Extractor(self.llm, self.roster)
        self.summarizer = Summarizer(self.llm, self.tz)
        self.alliances = AllianceTracker(self.db)
        self.relationships = RelationshipTracker(self.db)
        self.game_state = GameStateTracker(self.db, season.start_date)

        self._last_daily: dt.date | None = None
        self._recent_for_context: list = []  # last few processed updates

    # --- lifecycle ----------------------------------------------------------
    async def setup_hook(self) -> None:
        await self.db.connect()
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

    def is_admin(self, interaction: discord.Interaction) -> bool:
        if self.settings.owner_id and interaction.user.id == self.settings.owner_id:
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.administrator)

    def _in_season(self) -> bool:
        """True once the premiere date (from season.yaml) has arrived. Before that
        the bot ingests in the background but posts nothing to the channel."""
        return dt.datetime.now(self.tz).date() >= self.season.start_date

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

    # --- loops --------------------------------------------------------------
    @tasks.loop(minutes=2)
    async def ingest_loop(self) -> None:
        try:
            new_updates = await self.pipeline.run()
            if not new_updates:
                return

            channel = await self.update_channel()
            if channel and self._in_season():
                for u in new_updates:
                    if is_urgent(u):
                        desc = u.text[:1400]
                        if u.link:
                            desc += f"\n\n[source]({u.link})"
                        await channel.send(embed=discord.Embed(
                            title="🚨 Breaking", description=desc,
                            color=0xE74C3C, timestamp=dt.datetime.now(self.tz)))

            if self.llm.available and not self.roster.is_empty:
                context = await self.house_context()
                extraction = await self.extractor.extract(
                    new_updates,
                    context_updates=self._recent_for_context[-10:],
                    house_context=context,
                )
                await self.alliances.ingest(extraction.alliances)
                await self.relationships.ingest(extraction.relationships)
                await self.game_state.ingest(extraction.game_events)

            # Keep a small rolling window for the next extraction's context.
            self._recent_for_context = (self._recent_for_context + new_updates)[-10:]
        except Exception as e:
            log.error("ingest loop error: %s", e)

    @tasks.loop(minutes=60)
    async def hourly_loop(self) -> None:
        try:
            now = dt.datetime.now(self.tz)
            hour_end = now.replace(minute=0, second=0, microsecond=0)
            hour_start = hour_end - dt.timedelta(hours=1)
            start_utc = hour_start.astimezone(dt.timezone.utc)
            end_utc = hour_end.astimezone(dt.timezone.utc)
            updates = await self.db.updates_between(start_utc, end_utc)
            if not updates:
                return  # quiet hour: post nothing, store nothing

            context = await self.house_context()
            label = hour_end.strftime("%I %p").lstrip("0")
            embeds = await self.summarizer.hourly(updates, label, context)

            # Map step: store the digest so the daily recap covers the whole
            # day instead of a lossy top-5 of raw updates.
            if embeds and embeds[0].description:
                await self.db.add_summary("hourly", start_utc, end_utc,
                                          embeds[0].description, len(updates))

            if not self._in_season():
                return  # off-season: summarize/store silently, post nothing
            channel = await self.update_channel()
            if not channel:
                return
            for embed in embeds:
                await channel.send(embed=embed)
        except Exception as e:
            log.error("hourly loop error: %s", e)

    @tasks.loop(minutes=15)
    async def daily_loop(self) -> None:
        """Clock-checked so it fires at 06:00 in the configured timezone."""
        try:
            now = dt.datetime.now(self.tz)
            if now.hour != 6 or self._last_daily == now.date():
                return
            self._last_daily = now.date()

            if not self._in_season():
                return  # off-season: no daily recap until the season starts

            dissolved = await self.alliances.decay()
            if dissolved:
                log.info("daily decay dissolved %d alliances", dissolved)

            channel = await self.update_channel()
            if not channel:
                return

            end_utc = now.astimezone(dt.timezone.utc)
            start_utc = end_utc - dt.timedelta(hours=24)
            hourlies = await self.db.summaries_between("hourly", start_utc, end_utc)
            fallback = await self.db.recent_updates(24)
            context = await self.house_context()
            embed = await self.summarizer.daily_recap(
                hourlies, fallback, self.game_state.current_day(now.date()), context)
            await channel.send(embed=embed)
            if embed.description:
                await self.db.add_summary("daily", start_utc, end_utc,
                                          embed.description,
                                          sum(h["update_count"] for h in hourlies) or len(fallback))
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
