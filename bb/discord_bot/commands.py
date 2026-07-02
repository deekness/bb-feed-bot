"""Slash commands.

Public: /wtf, /summary, /alliances, /relationship, /gamestate
Admin:  /confirmalliance, /rejectalliance, /setchannel, /status
Owner:  /sync

This is intentionally small. Add new feature cogs (e.g. predictions) as
separate files and load them in setup_hook.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("bb.commands")


class BBCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- public -------------------------------------------------------------
    @app_commands.command(name="wtf", description="What's happening in the house right now?")
    async def wtf(self, interaction: discord.Interaction):
        await interaction.response.defer()
        updates = await self.bot.db.recent_updates(24)
        context = await self.bot.house_context()
        embed = await self.bot.summarizer.whats_happening(updates, context)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="summary", description="Summarize the last N hours (default 24).")
    async def summary(self, interaction: discord.Interaction, hours: int = 24):
        if not 1 <= hours <= 168:
            await interaction.response.send_message("Hours must be 1–168.", ephemeral=True)
            return
        await interaction.response.defer()
        updates = await self.bot.db.recent_updates(hours)
        context = await self.bot.house_context()
        embed = await self.bot.summarizer.whats_happening(updates, context)
        embed.title = f"Summary — last {hours}h"
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="alliances", description="Show currently tracked alliances.")
    async def alliances(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = await self.bot.alliances.active()
        embed = discord.Embed(title="🤝 Tracked Alliances",
                              color=0x3498DB, timestamp=discord.utils.utcnow())
        if not rows:
            embed.description = "No alliances detected yet."
        else:
            strong = [a for a in rows if a["confidence"] >= 0.6 or a["locked"]]
            weak = [a for a in rows if a not in strong]
            if strong:
                embed.add_field(name="Established", inline=False, value="\n".join(
                    self._fmt_alliance(a) for a in strong[:8]))
            if weak:
                embed.add_field(name="Suspected", inline=False, value="\n".join(
                    self._fmt_alliance(a) for a in weak[:6]))
            embed.set_footer(text="Confidence rises with corroboration and decays without it. "
                                  "Admins can /confirmalliance or /rejectalliance.")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="relationship", description="Show a houseguest's relationships.")
    async def relationship(self, interaction: discord.Interaction, houseguest: str):
        await interaction.response.defer()
        name = self.bot.roster.resolve(houseguest)
        if not name:
            await interaction.followup.send(
                f"'{houseguest}' isn't on the roster.", ephemeral=True)
            return
        rels = await self.bot.relationships.for_houseguest(name)
        embed = discord.Embed(title=f"💫 {name}'s Relationships",
                              color=0xE91E63, timestamp=discord.utils.utcnow())
        if not rels:
            embed.description = "No tracked relationships yet."
        else:
            lines = []
            for r in rels[:12]:
                tag = f" ({r['label']})" if r["label"] else ""
                arrow = "📈" if r["affinity"] > 0 else "📉" if r["affinity"] < 0 else "➖"
                lines.append(f"{arrow} **{r['other']}**{tag} — {r['affinity']:+.2f}")
            embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="gamestate", description="Show the current week's game state.")
    async def gamestate(self, interaction: discord.Interaction):
        await interaction.response.defer()
        week = self.bot.game_state.current_week()
        state = await self.bot.game_state.current(week)
        embed = discord.Embed(title=f"🎯 Game State — Week {week}",
                              color=0xF1C40F, timestamp=discord.utils.utcnow())
        if not state:
            embed.description = "No game-state facts recorded for this week yet."
        else:
            labels = {"hoh": "👑 HOH", "nominee": "🪑 Nominees", "veto_winner": "💎 Veto",
                      "veto_used_on": "🔓 Veto used on", "evicted": "🚪 Evicted",
                      "replacement_nominee": "🔁 Replacement"}
            for role, names in state.items():
                embed.add_field(name=labels.get(role, role.title()),
                                value=", ".join(names), inline=True)
        await interaction.followup.send(embed=embed)

    # --- admin --------------------------------------------------------------
    @app_commands.command(name="confirmalliance", description="(Admin) Lock an alliance as real.")
    async def confirmalliance(self, interaction: discord.Interaction, alliance_id: int):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        ok = await self.bot.alliances.confirm(alliance_id)
        await interaction.response.send_message(
            f"{'✅ Confirmed' if ok else '❌ Not found'}: alliance #{alliance_id}", ephemeral=True)

    @app_commands.command(name="rejectalliance", description="(Admin) Dismiss a wrong alliance.")
    async def rejectalliance(self, interaction: discord.Interaction, alliance_id: int):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        ok = await self.bot.alliances.reject(alliance_id)
        await interaction.response.send_message(
            f"{'🗑️ Rejected' if ok else '❌ Not found'}: alliance #{alliance_id}", ephemeral=True)

    @app_commands.command(name="setgamestate", description="(Admin) Record a game-state fact (fix a miss).")
    @app_commands.describe(role="hoh / nominee / veto_winner / veto_used_on / evicted / replacement_nominee",
                           houseguest="Houseguest name", week="Week number (default: current)")
    async def setgamestate(self, interaction: discord.Interaction, role: str,
                           houseguest: str, week: int | None = None):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        role = role.strip().lower()
        valid = ("hoh", "nominee", "veto_winner", "veto_used_on", "evicted", "replacement_nominee")
        if role not in valid:
            await interaction.response.send_message(
                f"Role must be one of: {', '.join(valid)}", ephemeral=True)
            return
        name = self.bot.roster.resolve(houseguest)
        if not name:
            await interaction.response.send_message(
                f"'{houseguest}' isn't on the roster.", ephemeral=True)
            return
        await self.bot.game_state.set_fact(role, name, week)
        wk = week or self.bot.game_state.current_week()
        await interaction.response.send_message(
            f"✅ Set: week {wk} {role} = {name}", ephemeral=True)

    @app_commands.command(name="removegamestate", description="(Admin) Delete a wrong game-state fact.")
    @app_commands.describe(role="hoh / nominee / veto_winner / veto_used_on / evicted / replacement_nominee",
                           houseguest="Houseguest name", week="Week number (default: current)")
    async def removegamestate(self, interaction: discord.Interaction, role: str,
                              houseguest: str, week: int | None = None):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        name = self.bot.roster.resolve(houseguest) or houseguest.strip()
        ok = await self.bot.game_state.remove_fact(role.strip().lower(), name, week)
        wk = week or self.bot.game_state.current_week()
        await interaction.response.send_message(
            f"{'🗑️ Removed' if ok else '❌ Not found'}: week {wk} {role} / {name}", ephemeral=True)

    @app_commands.command(name="setchannel", description="(Admin) Set the channel for posts.")
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        await self.bot.db.kv_set("update_channel_id", channel.id)
        await interaction.response.send_message(f"Posts will go to {channel.mention}.", ephemeral=True)

    @app_commands.command(name="status", description="(Admin) Show bot status.")
    async def status(self, interaction: discord.Interaction):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        channel = await self.bot.update_channel()
        recent = await self.bot.db.recent_updates(1)
        embed = discord.Embed(title="Bot Status", color=0x2ECC71)
        embed.add_field(name="Season", value=f"{self.bot.season.name} (week {self.bot.game_state.current_week()})", inline=False)
        embed.add_field(name="Roster", value=f"{len(self.bot.roster.names)} houseguests", inline=True)
        embed.add_field(name="LLM", value="✅ on" if self.bot.llm.available else "❌ off (pattern mode)", inline=True)
        embed.add_field(name="Channel", value=channel.mention if channel else "not set", inline=True)
        embed.add_field(name="Updates (last hour)", value=str(len(recent)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- owner --------------------------------------------------------------
    @app_commands.command(name="sync", description="(Owner) Re-sync slash commands.")
    async def sync(self, interaction: discord.Interaction):
        if self.bot.settings.owner_id and interaction.user.id != self.bot.settings.owner_id:
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        synced = await self.bot.tree.sync()
        await interaction.followup.send(f"Synced {len(synced)} commands.", ephemeral=True)

    @staticmethod
    def _fmt_alliance(a: dict) -> str:
        lock = "🔒 " if a["locked"] else ""
        name = a["name"] or "/".join(a["members"])
        members = ", ".join(a["members"])
        return f"{lock}**#{a['id']} {name}** — {members}  ({a['confidence']:.0%})"
