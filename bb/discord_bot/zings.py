"""Zingbot — a comedic roast command for the server.

`/zing` roasts a Discord member, a HOUSEGUEST (roster-backed autocomplete), or —
with nothing specified — a random human victim.

Houseguest zings are game-aware: the real Zingbot roasts hamsters about what they
actually did in the house, so when the LLM is available we generate a zing from
the tracked house state (comp results, noms, showmances, alliances). Register is hard-R roast comedy for an adult server: crude, profane, innuendo-heavy,
free to mock gameplay, stupidity, ego, vanity and thirst. Vanity about looks is fair
game; the looks themselves are not. Permanently barred: protected characteristics
(race, gender, orientation, religion, disability), sexually explicit content,
body-shaming, and their real life outside the show. These are real people —
a roast, not a hate crime. Falls back to the generic templates if the LLM is down.

Every roast is capped with a randomly chosen ZING! sign-off in the style of
Big Brother's Zingbot, so each one lands like the real thing and the ending
varies for extra freshness.

The roasts are generic, template-style burns (a {name} slot) in the hard-R
roast-comedy register: crude, profane, and heavy on innuendo. They are NOT
sexually explicit, and they never punch at protected characteristics (race,
gender, orientation, religion, disability). Because they're generic, they
don't target anyone's real traits — it's a roast, not a hate crime.

Freshness: a shuffle-bag hands out every roast once before any repeats.
NOTE: random-target mode needs the Server Members privileged intent.
"""
from __future__ import annotations

import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# Each roast gets one of these appended, Zingbot-style.
ZING_SIGNOFFS = [
    "ZING!",
    "ZING! ZING!",
    "ZING! ZING! ZING!",
    "ZING! ZING ZING ZING!",
    "ZIIIIING!",
    "ZINGITTY ZING ZAP!",
    "ZOO-WEE-ZING!",
    "ZUH-ZUH-ZUH-ZUH-ZING!",
    "ZING-ZUH-ZING-ZING-ZING!",
    "BIG OL' ZING!",
    "ZING, BABY, ZING!",
    "BRUTAL ZING!",
    "DENIED! ZING!",
    "BOOM. ZING!",
    "TRIPLE ZING! ZING ZING ZING!",
    "MAZEL TOV, ZING!",
    "ZINGITY-ZANG-ZONG!",
    "ZING! POW! ZING!",
    "ZINGER DEPLOYED. ZING!",
    "ZING! ZWOOP! ZING!",
    "ZING! ZA-ZING! ZINGGGG!",
    "no notes. ZING!",
    "ZINNNNG, baby!",
    "FATALITY. ZING!",
]

ROASTS = [
  "{name} has the confidence of a reality TV winner and the judgment of someone who clicks every popup ad they see.",
  "{name} says they're an alpha. The only thing they're leading is the list of avoidable mistakes.",
  "Everyone has that one friend who makes bad decisions look effortless. Thanks for your service, {name}.",
  "If excuses burned calories, {name} would be absolutely shredded.",
  "{name} spends so much time talking about potential that scientists are considering it a renewable energy source.",
  "Nickname alert: \"Temu Superman.\" All the branding, none of the flight.",
  "{name} has mastered the art of being confidently incorrect.",
  "If self-awareness were a subscription service, {name}'s payment definitely bounced.",
  "{name} walks into every room like they own the place. The place is usually a disaster.",
  "Some people are hard to figure out. {name} isn't one of them.",
  "{name} has the strategic mind of a squirrel crossing six lanes of traffic.",
  "If bad timing paid dividends, {name} would retire tomorrow.",
  "{name} acts mysterious. In reality, nobody's curious enough to investigate.",
  "Nickname alert: \"The Human Loading Screen.\" A lot of waiting for very little payoff.",
  "{name} talks a big game for someone still stuck in the tutorial level.",
  "Every group needs comic relief. Unfortunately, {name} wasn't trying to be funny.",
  "{name}'s biggest accomplishment is surviving the consequences of their own decisions.",
  "If overestimating yourself were an Olympic event, {name} would take gold and still complain about the judges.",
  "{name} has all the charm of a software update at 2 AM.",
  "You know how everyone has hidden talents? {name} should keep looking.",
  "Nickname alert: \"Budget Maverick.\" The danger is real, but none of it is intentional.",
  "{name} has a face for radio and a personality for airplane mode.",
  "If common sense were currency, {name} couldn't afford parking.",
  "{name} spends so much time chasing attention you'd think it owed them money.",
  "Some people learn from mistakes. {name} prefers a subscription model.",
  "{name} has the reliability of a weather forecast written on a napkin.",
  "If confidence and competence ever meet, {name} should introduce them.",
  "{name} treats accountability like it's a contagious disease.",
  "Nickname alert: \"Captain Almost.\" Always close enough to brag, never close enough to prove it.",
  "{name} is living proof that enthusiasm and ability are not the same thing.",
  "If life came with patch notes, half of {name}'s would be bug fixes.",
  "{name} has all the swagger of a celebrity and all the results of a parking cone.",
  "Nobody works harder than {name}... at explaining why something isn't their fault.",
  "{name} somehow manages to be both extra and underwhelming.",
  "If poor judgment had a spokesperson, {name} would already have the endorsement deal.",
  "Nickname alert: \"The Participation Legend.\" Showing up is the strongest part of the resume.",
  "{name} has turned lowering expectations into an art form.",
  "If secondhand embarrassment generated electricity, {name} could power a city block.",
  "{name} thinks they're keeping everyone guessing. Trust me, we figured it out.",
  "Some people leave a lasting impression. {name} leaves a cautionary tale.",
  "{name}'s dating strategy appears to be confusing persistence with chemistry.",
  "If red flags were frequent flyer miles, {name} would travel for free.",
  "{name} has all the game of an unplugged arcade machine.",
  "Nickname alert: \"Clearance Rack Casanova.\" Technically available, but nobody's rushing.",
  "{name} talks about standards the way rich people talk about yachts: mostly in theory.",
  "If awkward moments were collectibles, {name} would own the complete set.",
  "{name} has the unique ability to make a simple situation require a meeting.",
  "The best thing about {name}'s plans is how quickly reality ends them.",
  "{name} has enough confidence for three people and enough judgment for half of one.",
  "Some people peak in high school. {name} is still circling the parking lot looking for the entrance.",
  "{name} has the confidence to start an argument and the facts to lose it immediately.",
  "If bad ideas were a loyalty program, {name} would have platinum status.",
  "{name} spends so much time hyping themselves up that you'd think tickets were on sale.",
  "Nickname alert: \"The Human Detour.\" Every path gets longer once they're involved.",
  "{name} has all the discipline of a toddler in a candy store.",
  "If accountability knocked on the door, {name} would pretend nobody was home.",
  "{name} treats every warning sign like a personal challenge.",
  "Some people think outside the box. {name} can't find the box.",
  "{name} has enough confidence to launch a startup and enough planning to sink it by lunch.",
  "If self-inflicted problems were an art form, {name} would be hanging in a museum.",
  "{name} walks around like they're the prize. The raffle was canceled.",
  "Nickname alert: \"Discount James Bond.\" Licensed to disappoint.",
  "{name} has the problem-solving skills of a smoke detector with dead batteries.",
  "If life is a journey, {name} keeps missing the exits.",
  "{name} makes simple mistakes with remarkable consistency.",
  "Some people have a five-year plan. {name} has a five-minute panic.",
  "{name} has all the grace of a shopping cart with a broken wheel.",
  "If poor judgment burned fuel, {name} could orbit the Earth.",
  "{name} somehow turns every shortcut into the scenic route.",
  "Nickname alert: \"Captain Recalculate.\" Wrong direction, full confidence.",
  "{name} treats preparation like it's optional DLC.",
  "If reality gave performance reviews, {name} would be nervous.",
  "{name} has the unique ability to make certainty look irresponsible.",
  "Some people raise the bar. {name} trips over it.",
  "{name} could complicate a glass of water.",
  "If confidence were horsepower, {name} would be a race car. Unfortunately, they're missing the engine.",
  "{name} talks about success the way children talk about becoming astronauts.",
  "Nickname alert: \"The Human Placeholder.\" Something was supposed to go here.",
  "{name} has mastered the art of showing up unprepared and acting surprised.",
  "If bad luck had a favorite customer, it would know {name} by name.",
  "{name} somehow makes every lesson repeat itself.",
  "Some people learn from history. {name} keeps trying the sequel.",
  "{name} has all the foresight of a blindfolded dart thrower.",
  "If excuses were cryptocurrency, {name} would be a billionaire.",
  "{name} enters every challenge like they're already winning. Reality disagrees.",
  "Nickname alert: \"The Premium Headache.\"",
  "{name} has the consistency of a weather app during a thunderstorm.",
  "If ambition alone worked, {name} would own three countries.",
  "{name} has turned wishful thinking into a full-time occupation.",
  "Some people inspire confidence. {name} inspires follow-up questions.",
  "{name} could lose a game of hide-and-seek while hiding alone.",
  "If common sense were downloadable, {name}'s internet must be out.",
  "{name} has enough optimism for ten people and enough planning for none.",
  "Nickname alert: \"Captain Side Quest.\" Nobody knows how we got here.",
  "{name} treats consequences like surprise guests.",
  "Some people are self-made. {name} should ask for a refund.",
  "{name} has the reaction time of a sloth reading terms and conditions.",
  "If awkward pauses paid rent, {name} would own property.",
  "{name} could make a victory lap feel premature.",
  "Nickname alert: \"The Human Speed Bump.\" Progress was happening.",
  "{name} talks about their grind so much you'd think results were optional.",
  "If confidence paid taxes, {name} would fund the government.",
  "{name} somehow manages to be late and unprepared at the same time.",
  "Some people bring solutions. {name} brings stories.",
  "{name} has all the urgency of a loading bar stuck at 99%.",
  "Nickname alert: \"Budget Rockstar.\" Mostly feedback and noise.",
  "{name} treats criticism like it's fake news.",
  "If bad takes were baseball cards, {name} would have a complete collection.",
  "{name} could make a winning position look challenging.",
  "Some people think before they speak. {name} likes surprises.",
  "{name} has all the reliability of a gas station horoscope.",
  "If confidence were oxygen, {name} would be a fire hazard.",
  "{name} somehow finds new and exciting ways to miss the point.",
  "Nickname alert: \"Captain Bare Minimum.\"",
  "{name} has enough enthusiasm to start projects and enough discipline to abandon them.",
  "Some people leave a legacy. {name} leaves a trail.",
  "{name} could turn a sure thing into a coin flip.",
  "If poor timing had a mascot, {name} would wear the costume.",
  "{name} approaches responsibility like it's a telemarketer.",
  "Nickname alert: \"The Human Draft Version.\"",
  "{name} has the confidence of an expert and the research habits of a guy reading headlines.",
  "Some people think strategically. {name} thinks eventually.",
  "{name} has all the precision of a blindfolded magician.",
  "If bad planning were a competitive event, {name} would forget the schedule.",
  "{name} keeps chasing greatness while tripping over basics.",
  "Nickname alert: \"Captain Unforced Error.\"",
  "{name} has a gift for making obvious advice seem complicated.",
  "Some people get ahead through talent. {name} gets ahead through technicalities.",
  "{name} could make a smooth landing feel like an emergency.",
  "If denial burned calories, {name} would be in championship shape.",
  "{name} has enough self-confidence to survive any setback and enough judgment to create them.",
  "Nickname alert: \"The Human Fine Print.\" There's always a catch.",
  "{name} treats preparation like a rumor.",
  "Some people rise to the occasion. {name} sends a representative.",
  "{name} has all the momentum of a treadmill.",
  "If avoidable mistakes were frequent flyer miles, {name} would never pay for travel.",
  "{name} could start a debate in an empty room.",
  "Nickname alert: \"Captain Technical Difficulty.\"",
  "{name} has enough ambition to climb mountains and enough planning to forget the map.",
  "Some people make memories. {name} makes stories that begin with 'you're not gonna believe this.'",
  "{name} has all the confidence of a genius and all the evidence of a suspect.",
  "If reality were a teammate, it would be exhausted.",
  "{name} could turn a layup into a trick shot.",
  "Nickname alert: \"The Human Disclaimer.\"",
  "{name} has the remarkable ability to make every lesson feel optional.",
  "Some people are diamonds in the rough. {name} is mostly rough.",
  "{name} treats good advice the way vampires treat sunlight.",
  "If stubbornness generated electricity, {name} could power a stadium.",
  "{name} has all the certainty of someone who definitely didn't read the instructions.",
  "Some people are unforgettable because they're exceptional. {name} just found a different route."
]


# Zingbot's register when roasting a real houseguest: their GAME, not their person.
_ZING_SYSTEM = (
    "You are Zingbot 3000 from Big Brother: a cheesy, savage cornball robot who "
    "roasts houseguests on the live feeds. Write ONE zing (1-2 sentences) about the "
    "named houseguest — hard setup, harder punchline.\n"
    "AUDIENCE: an adult Discord server. The register is hard-R roast comedy: crude, "
    "profane (swearing is fine — the kind the network would bleep), filthy innuendo, "
    "genuinely brutal. Do not be gentle, do not hedge, do not soften the landing. A "
    "zing that does not sting is not a zing.\n"
    "FAIR GAME: their gameplay, garbage strategy, stupidity and terrible reads, ego, "
    "vanity, delusion, paranoia, thirst, desperation, showmance horniness, broken "
    "promises, comp failures, and how badly they are being played. Vanity IS fair "
    "game — the mirror-checker, the flexer, the one whose whole personality is "
    "thinking they're hot. Mock the EGO about their looks, never the looks.\n"
    "OFF LIMITS — hard rules, no exceptions:\n"
    "- Nothing about race, ethnicity, gender, sexual orientation, religion, or "
    "disability.\n"
    "- Sexual innuendo is fine; sexually explicit description of them is not.\n"
    "- No body-shaming: never mock their actual body, face, weight, or looks as a "
    "defect. Their vanity is a target; their appearance is not.\n"
    "- Nothing about their real life outside the show: family, trauma, private "
    "matters.\n"
    "These are real people. Roast the player and the ego, never their identity or "
    "their body. It is a roast, not a hate crime.\n"
    "MATERIAL: use the HOUSE STATE below so the zing lands on something real. If "
    "there is little to go on, roast them generically — never invent events.\n"
    "Do NOT add a ZING! sign-off; that gets appended for you. Output the zing only."
)


class ZingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._bag: list[int] = []  # shuffle-bag: every roast used once before repeats

    def _next_line(self, name: str) -> str:
        if not self._bag:
            self._bag = list(range(len(ROASTS)))
            random.shuffle(self._bag)
        roast = ROASTS[self._bag.pop()].format(name=name)
        return f"{roast}  {random.choice(ZING_SIGNOFFS)}"

    async def _houseguest_line(self, name: str) -> str:
        """Game-aware zing from tracked house state; generic template if no LLM."""
        llm = getattr(self.bot, "llm", None)
        if not (llm and llm.available):
            return self._next_line(name)
        try:
            context = await self.bot.house_context()
            user = (f"HOUSE STATE: {context}\n\n" if context else "") + \
                   f"Zing this houseguest: {name}"
            text = await llm.text(_ZING_SYSTEM, user, max_tokens=150)
        except Exception:
            text = None
        if not text:
            return self._next_line(name)
        return f"{text.strip()}  {random.choice(ZING_SIGNOFFS)}"

    async def houseguest_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        names = getattr(self.bot.roster, "names", [])
        cur = current.lower()
        return [app_commands.Choice(name=n, value=n)
                for n in sorted(names) if cur in n.lower()][:25]

    @app_commands.command(
        name="zing",
        description="Let Zingbot roast someone — a server member, a houseguest, or a random victim.",
    )
    @app_commands.describe(
        target="A Discord member to roast.",
        houseguest="A Big Brother houseguest to roast (game-aware zing).",
    )
    @app_commands.autocomplete(houseguest=houseguest_autocomplete)
    async def zing(self, interaction: discord.Interaction,
                   target: Optional[discord.Member] = None,
                   houseguest: Optional[str] = None):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Zingbot only works inside a server.", ephemeral=True)
            return
        if target and houseguest:
            await interaction.response.send_message(
                "Pick one victim at a time — a member *or* a houseguest.", ephemeral=True)
            return

        # --- houseguest zing (roster-gated, LLM-written when possible) ---
        if houseguest:
            canon = self.bot.roster.resolve(houseguest)
            if not canon:
                await interaction.response.send_message(
                    f"'{houseguest}' isn't on the roster. Try the autocomplete.",
                    ephemeral=True)
                return
            await interaction.response.defer()   # LLM call can take a couple seconds
            line = await self._houseguest_line(canon)
            embed = discord.Embed(
                title="🤖  ZINGBOT",
                description=f"**{canon}**\n\n{line}",
                color=0xE91E63,
            )
            embed.set_footer(text="🤖 beep boop — get zinged")
            await interaction.followup.send(embed=embed)
            return

        # --- member zing (unchanged behavior) ---
        if target is None:
            humans = [m for m in interaction.guild.members if not m.bot]
            if not humans:
                await interaction.response.send_message(
                    "Zingbot couldn't find anyone to roast. (Make sure the **Server Members** "
                    "intent is enabled.)", ephemeral=True)
                return
            target = random.choice(humans)

        embed = discord.Embed(
            title="🤖  ZINGBOT",
            description=f"{target.mention}\n\n{self._next_line(target.display_name)}",
            color=0xE91E63,
        )
        embed.set_footer(text="🤖 beep boop — get zinged")
        await interaction.response.send_message(embed=embed)
