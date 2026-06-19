"""The Discord bot: wires every component together and runs the loops.

Loops:
  * ingest (every 2 min)   -> pull sources, store new, extract, feed trackers,
                              post urgent moments immediately.
  * hourly (top of hour)   -> post a neutral hour summary built from the DB.
  * daily  (06:00 local)   -> post a day recap and decay stale alliances.

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

    # --- loops --------------------------------------------------------------
    @tasks.loop(minutes=2)
    async def ingest_loop(self) -> None:
        try:
            new_updates = await self.pipeline.run()
            if not new_updates:
                return

            channel = await self.update_channel()
            if channel:
                for u in new_updates:
                    if is_urgent(u):
                        await channel.send(embed=discord.Embed(
                            title="🚨 Breaking", description=u.text[:1500],
                            color=0xE74C3C, timestamp=dt.datetime.now(self.tz)))

            if self.llm.available and not self.roster.is_empty:
                extraction = await self.extractor.extract(new_updates)
                src_hash = new_updates[0].content_hash
                await self.alliances.ingest(extraction.alliances, src_hash)
                await self.relationships.ingest(extraction.relationships)
                await self.game_state.ingest(extraction.game_events, src_hash)
        except Exception as e:
            log.error("ingest loop error: %s", e)

    @tasks.loop(minutes=60)
    async def hourly_loop(self) -> None:
        try:
            channel = await self.update_channel()
            if not channel:
                return
            now = dt.datetime.now(self.tz)
            hour_end = now.replace(minute=0, second=0, microsecond=0)
            hour_start = hour_end - dt.timedelta(hours=1)
            updates = await self.db.updates_between(
                hour_start.astimezone(dt.timezone.utc),
                hour_end.astimezone(dt.timezone.utc))
            in_season = dt.datetime.now(self.tz).date() >= self.season.start_date
            if not updates and not in_season:
                return  # off-season quiet hour: stay silent (in-season posts hourly regardless)
            label = hour_end.strftime("%I %p").lstrip("0")
            for embed in await self.summarizer.hourly(updates, label):
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

            dissolved = await self.alliances.decay()
            if dissolved:
                log.info("daily decay dissolved %d alliances", dissolved)

            channel = await self.update_channel()
            if not channel:
                return
            updates = await self.db.recent_updates(24)
            if not updates and now.date() < self.season.start_date:
                return  # off-season: stay quiet
            embed = await self.summarizer.whats_happening(updates)
            embed.title = f"Day {self.game_state.current_day(now.date())} Recap"
            await channel.send(embed=embed)
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
