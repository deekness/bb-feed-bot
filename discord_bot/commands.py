"""Slash commands.

Public: /help, /wtf, /summary, /alliances, /alliance, /relationship,
        /gamestate, /ask, /votes, /houseguest, /week, /hamsters, /feeds, /episoderecap (+ /zing)
Admin:  /addhouseguest, /removehouseguest, /addnickname, /confirmalliance,
        /rejectalliance, /namealliance, /unlockalliance, /livewrites, /setgamestate, /removegamestate, /setchannel, /setrecapchannel, /setbriefingchannel, /setfeedschannel, /status,
        /testdm
Owner:  /sync

This is intentionally small. Add new feature cogs (e.g. predictions) as
separate files and load them in setup_hook.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..analysis.summarize import sentence_clamp as _sent_clamp
from ..analysis.summarize import one_sided_note as _one_sided_note

log = logging.getLogger("bb.commands")


def _chunk_lines(lines: list[str], limit: int = 1024) -> list[str]:
    """Pack lines into embed-field-sized chunks (Discord caps fields at 1024)."""
    chunks: list[str] = []
    cur = ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > limit:
            chunks.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        chunks.append(cur)
    return chunks


class BBCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- public -------------------------------------------------------------
    @app_commands.command(name="help", description="List everything the bot can do.")
    async def help(self, interaction: discord.Interaction):
        """Auto-generated from the command tree, so new cogs/commands show up
        without touching this. Admin/Owner sections render only for admins,
        keyed off the '(Admin)'/'(Owner)' description prefix convention."""
        public: list[str] = []
        admin: list[str] = []
        owner: list[str] = []
        for cmd in sorted(self.bot.tree.get_commands(), key=lambda c: c.name):
            desc = cmd.description or ""
            if desc.startswith("(Admin)"):
                admin.append(f"**/{cmd.name}** — {desc[7:].strip()}")
            elif desc.startswith("(Owner)"):
                owner.append(f"**/{cmd.name}** — {desc[7:].strip()}")
            else:
                public.append(f"**/{cmd.name}** — {desc}")

        embed = discord.Embed(title="📖 Bot Commands", color=0x5865F2)
        for i, chunk in enumerate(_chunk_lines(public)):
            embed.add_field(name="Everyone" if i == 0 else "Everyone (cont.)",
                            value=chunk, inline=False)
        if self.bot.is_admin(interaction):
            for i, chunk in enumerate(_chunk_lines(admin)):
                embed.add_field(name="Admin" if i == 0 else "Admin (cont.)",
                                value=chunk, inline=False)
            for i, chunk in enumerate(_chunk_lines(owner)):
                embed.add_field(name="Owner" if i == 0 else "Owner (cont.)",
                                value=chunk, inline=False)
        embed.set_footer(text="Hourly summaries, daily recaps (6am), weekly recaps, "
                              "and 🚨 Breaking posts arrive automatically.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="wtf", description="What's happening in the house right now?")
    async def wtf(self, interaction: discord.Interaction):
        await interaction.response.defer()
        updates, window_h, recent_count = await self.bot.wtf_updates()
        context = await self.bot.house_context()
        embed = await self.bot.summarizer.whats_happening(
            updates, context, window_hours=window_h, recent_count=recent_count)
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
        # IDs are admin plumbing (/confirmalliance takes one). They mean nothing
        # to everyone else, so they only render for admins.
        show_id = self.bot.is_admin(interaction)
        embed = discord.Embed(title="🤝 Tracked Alliances",
                              color=0x3498DB, timestamp=discord.utils.utcnow())
        if not rows:
            embed.description = "No alliances detected yet."
        else:
            strong = [a for a in rows if a["confidence"] >= 0.6 or a["locked"]]
            weak = [a for a in rows if a not in strong]
            if strong:
                embed.add_field(name="Established", inline=False, value="\n".join(
                    self._fmt_alliance(a, show_id) for a in strong[:8]))
            if weak:
                embed.add_field(name="Suspected", inline=False, value="\n".join(
                    self._fmt_alliance(a, show_id) for a in weak[:6]))
            foot = "Confidence rises with corroboration and decays without it."
            if show_id:
                foot += " Admins can /confirmalliance or /rejectalliance."
            embed.set_footer(text=foot)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="alliance", description="One alliance's details and the evidence behind it.")
    @app_commands.describe(alliance_id="The #id shown in /alliances")
    async def alliance(self, interaction: discord.Interaction, alliance_id: int):
        await interaction.response.defer()
        a = await self.bot.alliances.detail(alliance_id)
        if not a:
            await interaction.followup.send(f"No alliance #{alliance_id}.", ephemeral=True)
            return
        lock = "🔒 " if a["locked"] else ""
        embed = discord.Embed(
            title=f"🤝 {lock}#{a['id']} {a['name'] or '/'.join(a['members'])}",
            color=0x3498DB, timestamp=discord.utils.utcnow())
        embed.add_field(name="Members", value=", ".join(a["members"]), inline=False)
        embed.add_field(name="Status", value=a["status"], inline=True)
        embed.add_field(name="Confidence", value=f"{a['confidence']:.0%}", inline=True)
        embed.add_field(name="First seen", value=a["first_seen"].astimezone(self.bot.tz).strftime("%b %d %I:%M %p"), inline=True)
        evidence = await self.bot.alliances.evidence(alliance_id)
        if evidence:
            lines = []
            for e in evidence:
                ts = e["created_at"].astimezone(self.bot.tz).strftime("%b %d %I:%M %p")
                quote = _sent_clamp((e["quote"] or "").strip(), 150)
                line = f"[{ts}] “{quote}”" if quote else f"[{ts}] (no quote)"
                lines.append(line)
            embed.add_field(name=f"Evidence (latest {len(evidence)})",
                            value="\n".join(lines)[:1024], inline=False)
        if not a["locked"]:
            embed.set_footer(text=f"/confirmalliance {a['id']} to lock it in · /rejectalliance {a['id']} to dismiss")
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
                arrow = ("🔪" if r["label"] == "betrayed" else
                         "📈" if r["affinity"] > 0 else "📉" if r["affinity"] < 0 else "➖")
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

    @app_commands.command(name="ask", description="Ask a question about anything that happened on the feeds.")
    @app_commands.describe(question="e.g. 'Why are Sarah and Mike fighting?'")
    async def ask(self, interaction: discord.Interaction, question: str):
        if not 3 <= len(question) <= 300:
            await interaction.response.send_message("Question must be 3–300 characters.", ephemeral=True)
            return
        await interaction.response.defer()
        import datetime as _dt
        matches = await self.bot.db.search_updates(question, limit=40)
        end = _dt.datetime.now(_dt.timezone.utc)
        dailies = await self.bot.db.summaries_between("daily", end - _dt.timedelta(days=7), end)
        context = await self.bot.house_context()
        embed = await self.bot.summarizer.ask(question, matches, dailies, context)
        await interaction.followup.send(embed=embed)

    _FIRM_MARK = {"locked": "🔒", "unsure": "❔", "leaning": ""}

    def _voters_line(self, voters: list[tuple[str, str]]) -> str:
        m = self._FIRM_MARK
        return ", ".join(f"{v}{(' ' + m[f]) if m.get(f) else ''}"
                         for v, f in sorted(voters)) or "—"

    @app_commands.command(name="votes", description="Where the eviction votes stand this week.")
    async def votes(self, interaction: discord.Interaction):
        await interaction.response.defer()
        week = self.bot.game_state.current_week()
        state = await self.bot.game_state.current(week)
        saved_by_veto = set(state.get("veto_used_on", []))
        noms = [n for n in (state.get("nominee", []) +
                            state.get("replacement_nominee", []))
                if n not in saved_by_veto]
        # de-dup, preserve order
        noms = list(dict.fromkeys(noms))

        embed = discord.Embed(title=f"🗳️ Vote Count — Week {week}",
                              color=0xE67E22, timestamp=discord.utils.utcnow())

        # BB28's Block Buster: with three on the block, the final pair is
        # unknown until eviction night — a single flat count is misleading.
        # Show each outcome instead.
        if len(noms) == 3:
            plans = await self.bot.votes.plans(week)
            if not plans:
                embed.description = "No evidenced vote plans tracked yet this week."
                await interaction.followup.send(embed=embed)
                return
            embed.description = (
                f"Three on the block: **{', '.join(noms)}**. The Block Buster "
                "winner escapes and votes — each outcome below.")
            for saved in noms:
                pair = tuple(n for n in noms if n != saved)
                board = self.bot.votes.scenario_board(plans, pair, saved)
                lines = []
                for tgt in pair:
                    voters = board[tgt]
                    lines.append(f"**Evict {tgt} — {len(voters)}**  "
                                 f"{self._voters_line(voters)}")
                if board["?"]:
                    lines.append(f"❔ unclear: {self._voters_line(board['?'])}")
                embed.add_field(
                    name=f"If {saved} wins the Block Buster → {pair[0]} vs {pair[1]}",
                    value="\n".join(lines), inline=False)
            embed.set_footer(text="Ranked plans: a voter's fallback counts when their first "
                                  "choice escapes. 🔒 locked · ❔ unsure/unclear. "
                                  "Houseguests flip — snapshot, not a lock.")
            await interaction.followup.send(embed=embed)
            return

        counts = await self.bot.votes.current(week)
        if not counts:
            embed.description = "No evidenced vote plans tracked yet this week."
        else:
            ranked = sorted(counts.items(), key=lambda kv: len(kv[1]), reverse=True)
            for target, voters in ranked:
                embed.add_field(name=f"To evict {target} — {len(voters)}",
                                value=self._voters_line(voters), inline=False)
            embed.set_footer(text="Latest stated plan per voter since the last eviction. "
                                  "🔒 locked · ❔ unsure. Houseguests flip — snapshot, not a lock.")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="houseguest", description="Everything tracked about one houseguest.")
    async def houseguest(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        hg = self.bot.roster.resolve(name)
        if not hg:
            await interaction.followup.send(f"'{name}' isn't on the roster.", ephemeral=True)
            return
        embed = discord.Embed(title=f"👤 {hg}", color=0x2980B9,
                              timestamp=discord.utils.utcnow())

        rows = await self.bot.db.fetch(
            "SELECT week, role FROM game_state WHERE houseguest = $1 ORDER BY week, role", hg)
        if rows:
            comp = "\n".join(f"Week {r['week']}: {r['role'].replace('_', ' ')}" for r in rows[:15])
            embed.add_field(name="Game history", value=comp, inline=False)

        alliances = await self.bot.alliances.for_houseguest(hg)
        if alliances:
            lines = []
            for a in alliances[:6]:
                lock = "🔒 " if a["locked"] else ""
                label = a["name"] or "/".join(a["members"])
                lines.append(f"{lock}**{label}** — {', '.join(a['members'])} ({a['confidence']:.0%})")
            embed.add_field(name="Alliances", value="\n".join(lines), inline=False)

        rels = await self.bot.relationships.for_houseguest(hg)
        if rels:
            lines = []
            for r in rels[:8]:
                tag = f" ({r['label']})" if r["label"] else ""
                arrow = ("🔪" if r["label"] == "betrayed" else
                         "📈" if r["affinity"] > 0 else "📉" if r["affinity"] < 0 else "➖")
                lines.append(f"{arrow} **{r['other']}**{tag} {r['affinity']:+.2f}")
            embed.add_field(name="Relationships", value="\n".join(lines), inline=False)

        mentions = await self.bot.db.count_mentions(hg, 7)
        embed.set_footer(text=f"Mentioned in {mentions} feed updates over the last 7 days")
        if not rows and not alliances and not rels:
            embed.description = "Nothing tracked yet."
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="week", description="Recap of a full game week (default: last completed).")
    async def week(self, interaction: discord.Interaction, number: int | None = None):
        await interaction.response.defer()
        import datetime as _dt
        current = self.bot.game_state.current_week()
        number = number or max(1, current - 1)
        if not 1 <= number <= current:
            await interaction.followup.send(f"Week must be 1–{current}.", ephemeral=True)
            return
        start_date = self.bot.season.start_date + _dt.timedelta(days=7 * (number - 1))
        end_date = start_date + _dt.timedelta(days=7)
        tz = self.bot.tz
        start = _dt.datetime.combine(start_date, _dt.time.min, tz).astimezone(_dt.timezone.utc)
        end = _dt.datetime.combine(end_date, _dt.time.max, tz).astimezone(_dt.timezone.utc)
        dailies = await self.bot.db.summaries_between("daily", start, end)
        context = await self.bot.house_context()
        embed = await self.bot.summarizer.weekly_recap(dailies, number, context)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="episoderecap",
                          description="Recap the most recent Big Brother episode.")
    async def episoderecap(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rec = self.bot._recent_episode()
        if not rec:
            await interaction.followup.send(
                "No aired episode found in the last week to recap.", ephemeral=True)
            return
        _key, start_utc, end_utc, label = rec
        embed = await self.bot._generate_episode_recap(
            start_utc, end_utc, label, force=True)
        if not embed:
            await interaction.followup.send(
                f"No updates were captured during the {label} episode window "
                "to recap.", ephemeral=True)
            return
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="feeds", description="Are the live feeds LIVE right now, or on Anipals/WBRB?")
    async def feeds(self, interaction: discord.Interaction):
        st = await self.bot.db.kv_get("feed_state") or {}
        if not st.get("state"):
            await interaction.response.send_message(
                "No feed-state signal yet. The upstream tracker "
                "(@feed-bot.bsky.social / feedbot.liquid8d.dev) may not be running "
                "for this season yet.", ephemeral=True)
            return
        import datetime as _dt
        pretty = {"live": ("🟢", "LIVE"), "anipals": ("🐾", "Anipals"),
                  "wbrb": ("⏸️", "WBRB")}
        emoji, label = pretty.get(st["state"], ("❔", st["state"]))
        embed = discord.Embed(title=f"{emoji} Feeds: {label}", color=0x3498DB)
        try:
            since = _dt.datetime.fromisoformat(st["since"])
            embed.add_field(name="Since", value=f"<t:{int(since.timestamp())}:R>",
                            inline=True)
        except (KeyError, ValueError, TypeError):
            pass
        if st.get("text"):
            src = _sent_clamp(st["text"], 200)
            embed.add_field(name="Latest signal", value=src, inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="hamsters", description="Show the current season roster and nicknames.")
    async def hamsters(self, interaction: discord.Interaction):
        r = self.bot.roster
        embed = discord.Embed(title=f"📋 {self.bot.season.name} Roster",
                              color=0x2ECC71, timestamp=discord.utils.utcnow())
        if r.is_empty:
            embed.description = ("Roster is empty — extraction is paused until it's filled. "
                                 "Admins can /addhouseguest as the cast is revealed.")
        else:
            embed.description = ", ".join(sorted(r.names))
            nicks = r.nicknames
            if nicks:
                nick_text = ", ".join(f"{k} → {v}" for k, v in sorted(nicks.items()))
                embed.add_field(name="Nicknames", value=nick_text[:1024], inline=False)
            embed.set_footer(text=f"{len(r.names)} houseguests")
        await interaction.response.send_message(embed=embed)

    # --- admin --------------------------------------------------------------
    @app_commands.command(name="addhouseguest", description="(Admin) Add a houseguest to the roster — no redeploy needed.")
    @app_commands.describe(name="Canonical first name as the feeds use it, e.g. 'Rachel'")
    async def addhouseguest(self, interaction: discord.Interaction, name: str):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        name = name.strip()
        if not 1 <= len(name) <= 40:
            await interaction.response.send_message("Name must be 1–40 characters.", ephemeral=True)
            return
        if self.bot.roster.contains(name):
            await interaction.response.send_message(
                f"'{self.bot.roster.resolve(name)}' is already on the roster.", ephemeral=True)
            return
        self.bot.roster.add(name)
        extra = await self.bot.db.kv_get("roster_extra") or []
        if name not in extra:
            extra.append(name)
        await self.bot.db.kv_set("roster_extra", extra)
        # Un-remove if it was previously removed.
        removed = [n for n in (await self.bot.db.kv_get("roster_removed") or [])
                   if n.lower() != name.lower()]
        await self.bot.db.kv_set("roster_removed", removed)
        await interaction.response.send_message(
            f"✅ Added **{name}** — roster is now {len(self.bot.roster.names)} houseguests. "
            "Extraction and Bluesky relevance pick this up immediately.", ephemeral=True)

    @app_commands.command(name="removehouseguest", description="(Admin) Remove a mistaken roster entry (evicted HGs should STAY).")
    @app_commands.describe(name="Name to remove — typo fixes only")
    async def removehouseguest(self, interaction: discord.Interaction, name: str):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        canon = self.bot.roster.resolve(name)
        if not canon:
            await interaction.response.send_message(f"'{name}' isn't on the roster.", ephemeral=True)
            return
        self.bot.roster.remove(canon)
        extra = [n for n in (await self.bot.db.kv_get("roster_extra") or [])
                 if n.lower() != canon.lower()]
        await self.bot.db.kv_set("roster_extra", extra)
        removed = await self.bot.db.kv_get("roster_removed") or []
        if canon not in removed:
            removed.append(canon)
        await self.bot.db.kv_set("roster_removed", removed)
        await interaction.response.send_message(
            f"🗑️ Removed **{canon}** — roster is now {len(self.bot.roster.names)} houseguests.",
            ephemeral=True)

    @app_commands.command(name="addnickname", description="(Admin) Map a nickname/typo the feeds use to a roster name.")
    @app_commands.describe(nickname="What the feeds call them", target="Canonical roster name")
    async def addnickname(self, interaction: discord.Interaction, nickname: str, target: str):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        canon = self.bot.roster.resolve(target)
        if not canon:
            await interaction.response.send_message(
                f"'{target}' isn't on the roster — add them first with /addhouseguest.",
                ephemeral=True)
            return
        nickname = nickname.strip()
        if not 1 <= len(nickname) <= 40:
            await interaction.response.send_message("Nickname must be 1–40 characters.", ephemeral=True)
            return
        self.bot.roster.add_nickname(nickname, canon)
        nicks = await self.bot.db.kv_get("nickname_extra") or {}
        nicks[nickname.lower()] = canon
        await self.bot.db.kv_set("nickname_extra", nicks)
        await interaction.response.send_message(
            f"✅ '{nickname}' now resolves to **{canon}**.", ephemeral=True)

    @app_commands.command(name="confirmalliance", description="(Admin) Lock an alliance as real.")
    async def confirmalliance(self, interaction: discord.Interaction, alliance_id: int):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        ok = await self.bot.alliances.confirm(alliance_id)
        await interaction.response.send_message(
            f"{'✅ Confirmed' if ok else '❌ Not found'}: alliance #{alliance_id}", ephemeral=True)

    @app_commands.command(name="namealliance",
                          description="(Admin) Set or clear an alliance's name.")
    @app_commands.describe(alliance_id="The #id shown in /alliances (admins see these)",
                           name="The alliance's name. Leave empty to clear a wrong one.")
    async def namealliance(self, interaction: discord.Interaction,
                           alliance_id: int, name: str | None = None):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        clean = (name or "").strip()[:60] or None
        ok = await self.bot.alliances.rename(alliance_id, clean)
        if not ok:
            await interaction.response.send_message(
                f"No alliance #{alliance_id}.", ephemeral=True)
            return
        msg = (f"✏️ Alliance #{alliance_id} is now **{clean}**." if clean
               else f"✏️ Cleared the name on alliance #{alliance_id} — it'll show "
                    "by members until the feeds reveal a real one.")
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="unlockalliance",
                          description="(Admin) Hand an alliance back to automatic tracking.")
    @app_commands.describe(alliance_id="The #id shown in /alliances")
    async def unlockalliance(self, interaction: discord.Interaction, alliance_id: int):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        ok = await self.bot.alliances.unlock(alliance_id)
        await interaction.response.send_message(
            (f"🔓 Alliance #{alliance_id} is back under automatic tracking — its "
             "confidence, promotion, decay and dissolution will all update from "
             "the feeds again." if ok else f"No alliance #{alliance_id}."),
            ephemeral=True)

    @app_commands.command(name="rejectalliance", description="(Admin) Dismiss a wrong alliance.")
    async def rejectalliance(self, interaction: discord.Interaction, alliance_id: int):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        ok = await self.bot.alliances.reject(alliance_id)
        await interaction.response.send_message(
            f"{'🗑️ Rejected' if ok else '❌ Not found'}: alliance #{alliance_id}", ephemeral=True)

    @app_commands.command(name="livewrites",
                          description="(Admin) Pause/resume game-state & vote recording (use while feeds are off).")
    @app_commands.describe(state="'paused' = don't record HOH/noms/evictions/votes (pre-feeds or outage); 'live' = normal")
    @app_commands.choices(state=[
        app_commands.Choice(name="paused (feeds off — hold facts & rumors)", value="paused"),
        app_commands.Choice(name="live (feeds on — record normally)", value="live"),
    ])
    async def livewrites(self, interaction: discord.Interaction,
                         state: app_commands.Choice[str]):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        paused = state.value == "paused"
        await self.bot.db.kv_set("live_writes_paused", paused)
        if paused:
            msg = ("⏸️ **Game-state & vote recording paused.** HOH, nominations, "
                   "evictions, and vote plans will NOT be written while feeds are "
                   "off — rumors like an unaired HOH win are ignored. Alliances and "
                   "relationships still track. Run `/livewrites live` once feeds "
                   "are live.")
        else:
            msg = ("▶️ **Game-state & vote recording resumed.** Live feed facts will "
                   "be recorded normally again.")
        await interaction.response.send_message(msg, ephemeral=True)

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

    @app_commands.command(name="testdm", description="(Admin) Send a test DM to verify admin nudges can reach the owner.")
    async def testdm(self, interaction: discord.Interaction):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if not self.bot.settings.owner_id:
            await interaction.followup.send(
                "❌ `OWNER_ID` env var is not set on Railway — nudges have nowhere "
                "to go. Set it to your Discord user ID and redeploy.", ephemeral=True)
            return
        ok = await self.bot._send_admin_dm(discord.Embed(
            title="🔔 Test nudge", color=0xF39C12,
            description="Admin DMs are working. You'll get a daily to-do here "
                        "whenever something needs human review (roster gaps, "
                        "missing HOH/noms, alliances awaiting confirm/reject) "
                        "and an alert if the feeds stall mid-season."))
        if ok:
            await interaction.followup.send("✅ Sent — check your DMs.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"❌ Couldn't DM <@{self.bot.settings.owner_id}>. Usual cause: "
                "Discord privacy settings block DMs from server members — enable "
                "them for this server, or check that OWNER_ID is your user ID.",
                ephemeral=True)

    @app_commands.command(name="setchannel", description="(Admin) Set the channel for posts.")
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        await self.bot.db.kv_set("update_channel_id", channel.id)
        await interaction.response.send_message(f"Posts will go to {channel.mention}.", ephemeral=True)

    @app_commands.command(name="needtoknow",
                          description="Where the game stands right now.")
    async def needtoknow(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.bot.build_briefing()
        if not embed:
            await interaction.followup.send(
                "Not enough recent feed activity to build a briefing yet.",
                ephemeral=True)
            return
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="setfeedschannel",
        description="(Admin) Send feed-state alerts (feeds back/down) to a channel.")
    @app_commands.describe(channel="Where feed alerts go. Omit to use the main channel.")
    async def setfeedschannel(self, interaction: discord.Interaction,
                              channel: discord.TextChannel | None = None):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        if channel is None:
            await self.bot.db.kv_set("feeds_channel_id", None)
            await interaction.response.send_message(
                "Feed alerts will post in the main update channel again.",
                ephemeral=True)
            return
        await self.bot.db.kv_set("feeds_channel_id", channel.id)
        await interaction.response.send_message(
            f"📡 Feed-state alerts (feeds back / down) will post in "
            f"{channel.mention}.", ephemeral=True)

    @app_commands.command(
        name="setbriefingchannel",
        description="(Admin) Send the daily 'need to know' brief to a channel.")
    @app_commands.describe(channel="Where the brief goes. Omit to use the recap channel.")
    async def setbriefingchannel(self, interaction: discord.Interaction,
                                 channel: discord.TextChannel | None = None):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        if channel is None:
            await self.bot.db.kv_set("briefing_channel_id", None)
            await interaction.response.send_message(
                "Briefing will follow the recap channel again.", ephemeral=True)
            return
        await self.bot.db.kv_set("briefing_channel_id", channel.id)
        await interaction.response.send_message(
            f"📋 The daily 'need to know' brief will post in {channel.mention} "
            f"at {self.bot.BRIEFING_HOUR}:00 house time (Pacific).", ephemeral=True)

    @app_commands.command(
        name="setrecapchannel",
        description="(Admin) Send daily/weekly recaps to a different channel.")
    @app_commands.describe(channel="Where recaps go. Omit to send them back to the main channel.")
    async def setrecapchannel(self, interaction: discord.Interaction,
                              channel: discord.TextChannel | None = None):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        if channel is None:
            await self.bot.db.kv_set("recap_channel_id", None)
            await interaction.response.send_message(
                "Recaps will post in the main update channel again.", ephemeral=True)
            return
        await self.bot.db.kv_set("recap_channel_id", channel.id)
        await interaction.response.send_message(
            f"📅 Daily & weekly recaps will now post in {channel.mention}. "
            "Everything else (hourly summaries, 🚨 Breaking, feed state) stays in "
            "the main channel.", ephemeral=True)

    @app_commands.command(name="status", description="(Admin) Show bot status.")
    async def status(self, interaction: discord.Interaction):
        if not self.bot.is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        channel = await self.bot.update_channel()
        rchannel = await self.bot.recap_channel()
        recent = await self.bot.db.recent_updates(1)
        embed = discord.Embed(title="Bot Status", color=0x2ECC71)
        chans = f"updates: {channel.mention if channel else '—'}"
        if rchannel and channel and rchannel.id != channel.id:
            chans += f"\nrecaps: {rchannel.mention}"
        embed.add_field(name="Channels", value=chans, inline=False)
        embed.add_field(name="Season", value=f"{self.bot.season.name} (week {self.bot.game_state.current_week()})", inline=False)
        embed.add_field(name="Roster", value=f"{len(self.bot.roster.names)} houseguests", inline=True)
        if self.bot.llm.available:
            wm, rm = self.bot.llm.model, self.bot.llm.recap_model
            llm_val = f"✅ {wm}" + (f"\nrecap: {rm}" if rm != wm else "")
        else:
            llm_val = "❌ off (pattern mode)"
        embed.add_field(name="LLM", value=llm_val, inline=False)
        embed.add_field(name="Channel", value=channel.mention if channel else "not set", inline=True)
        embed.add_field(name="Updates (last hour)", value=str(len(recent)), inline=True)
        rss = getattr(self.bot, "rss_source", None)
        if rss is not None:
            if rss.consecutive_failures:
                rss_val = f"❌ down ({rss.consecutive_failures} fails)"
            elif rss.using_proxy:
                rss_val = "🔁 via proxy (Jokers blocks cloud IPs)"
            else:
                rss_val = "✅ direct"
            every = getattr(rss, "poll_interval_s", 0)
            if every:
                rss_val += f"\npolled every {every // 60}m"
            nm = getattr(rss, "not_modified_count", 0)
            if nm:
                rss_val += f"\n{nm} × 304 (no re-download)"
            embed.add_field(name="Jokers RSS", value=rss_val, inline=True)
        import datetime as _dt
        now = _dt.datetime.now(self.bot.tz)
        ep = self.bot.episode_now()
        clock = f"{self.bot.settings.timezone}\n{now.strftime('%a %I:%M %p')}"
        if ep:
            clock += " · 📺 episode window" + (" (LIVE)" if ep.get("live") else "")
        embed.add_field(name="Clock", value=clock, inline=True)
        paused = await self.bot.db.kv_get("live_writes_paused")
        fs = (await self.bot.db.kv_get("feed_state") or {}).get("state")
        if paused:
            writes = "⏸️ paused (feeds off)"
        elif fs in ("anipals", "wbrb"):
            writes = f"⏸️ auto-held (feeds {fs})"
        else:
            writes = "▶️ live"
        embed.add_field(name="Game/vote writes", value=writes, inline=True)
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
    def _fmt_alliance(a: dict, show_id: bool = True) -> str:
        lock = "🔒 " if a["locked"] else ""
        name = a["name"] or "/".join(a["members"])
        members = ", ".join(a["members"])
        tags = []
        if len(a["members"]) == 2:
            tags.append("final 2")
        note = _one_sided_note(a)
        if note:
            tags.append(f"⚠️ {note}")
        suffix = f"  _{' · '.join(tags)}_" if tags else ""
        tag = f"#{a['id']} " if show_id else ""
        return f"{lock}**{tag}{name}** — {members}  ({a['confidence']:.0%}){suffix}"
