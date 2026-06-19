"""Zingbot — a comedic roast command for the server.

`/zing` roasts a specific member, or (with no target) a random human victim.

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
    # --- sex / love life (raunchy) ---
    "{name} fucks like they're trying to return it for store credit.",
    "{name}'s sex life is so dry the Sahara sends thoughts and prayers.",
    "The last time {name} got touched by another person it was a TSA pat-down.",
    "{name}'s O-face is just their regular face finally giving up.",
    "{name} could walk into an orgy and leave with a firm handshake.",
    "{name} gives head like they're bobbing for the last apple at a funeral.",
    "{name}'s dick pics come with an apology and a magnifying glass.",
    "{name} has the sexual stamina of a sneeze.",
    "{name} finishes so fast their partner is still parking the car.",
    "{name}'s idea of foreplay is asking if it counts.",
    "{name} couldn't get laid in a room full of horny clones holding a sign that says 'free.'",
    "{name} has blue balls courtesy of their own personality.",
    "{name}'s sex tape is being studied as a cure for insomnia.",
    "{name}'s only ride tonight is the Uber home, alone, again.",
    "{name} screams the wrong name in bed and it's still their own.",
    "{name}'s nudes get stamped 'return to sender.'",
    "The only thing {name} eats out is a drive-thru.",
    "{name} is so bad in bed the mattress filed a complaint with HR.",
    "{name} could turn a bachelorette party into a candlelit vigil.",
    "{name} thinks 69 is just a bus route.",
    "{name}'s sexting is 90 percent typos and 10 percent regret.",
    "{name} married their right hand and it's already drafting a prenup.",
    "{name}'s pull-out game is strong because nobody's ever let them in.",
    "{name} thinks 'going down' is something the stock market does.",
    "{name} couldn't pull in a wind tunnel.",
    "{name}'s body count is in witness protection because it doesn't exist.",
    "{name} got cock-blocked by their own reflection.",
    "{name} once paid for a lap dance and got a refund out of pity.",
    "{name} couldn't seduce a mirror with a twenty taped to their forehead.",
    "{name}'s dirty talk is just them apologizing in advance.",
    "{name} keeps their virginity like it's vintage.",
    "{name}'s sex playlist is the Windows shutdown sound on loop.",
    "{name} fumbled a one-night stand into a no-night stand.",
    "{name} could make Viagra file a no-confidence vote.",
    "{name} got friend-zoned mid-hookup.",
    "{name}'s seduction technique was last seen repelling mosquitoes.",
    "{name} couldn't get a hand job from a clock.",
    "{name}'s love language is being left on read.",

    # --- profane savage ---
    "{name} is the human equivalent of stepping in shit while wearing socks.",
    "{name} is a walking advertisement for the morning-after pill.",
    "{name} is what happens when a broken condom unionizes.",
    "{name} fell out of the ugly tree and the tree got a restraining order.",
    "{name} is proof the gene pool needs a fucking lifeguard.",
    "{name} could make a saint mutter 'oh, fuck this.'",
    "{name} is a dumbass in a smart-casual fit.",
    "{name} is the kind of stupid that should come with a warning sticker.",
    "{name} couldn't pour piss out of a boot if the directions were tattooed on their dick.",
    "{name} is a cautionary tale their own mother tells at parties.",
    "{name} has two settings: wrong, and louder.",
    "{name} is living proof you can be a whole disappointment without even trying.",
    "{name} is the human version of biting tinfoil.",
    "{name} is the reason their family has a 'do not seat near' list.",
    "{name} is a shit sandwich and they brought the bread.",
    "{name} could fuck up a glass of water.",
    "{name} is what the warranty means by 'not covered.'",
    "{name} is a glorified inconvenience with a pulse.",
    "{name} is the reason the aliens keep driving past.",
    "{name} fucked up so consistently it's basically a personality trait.",
    "{name} is a clearance-rack human, marked down twice, still not moving.",

    # --- dumbass ---
    "{name} is so dumb they tripped over a wireless signal.",
    "{name} brought one brain cell to a knife fight and it ghosted them.",
    "{name} read at a third-grade level until the third grade filed a complaint.",
    "{name} thinks 'oral' is a toothpaste brand.",
    "{name} got an F in a class with no final.",
    "{name} is the reason safety scissors exist.",
    "{name} googles their own name to remember it.",
    "{name} has the survival instincts of a moth french-kissing a bug zapper.",
    "{name} could lose rock-paper-scissors to a brick wall.",
    "{name} thinks gaslighting is a utility bill.",
    "{name} thinks 'subtle' is a font.",
    "{name} couldn't find their own ass with both hands and a GPS.",
    "{name} reheats cereal.",

    # --- pathetic / loser ---
    "{name} peaked in the womb and it's been a fucking landslide ever since.",
    "{name} cried in a Wendy's and that wasn't even the low point of their week.",
    "{name}'s rock bottom called and begged them to stop digging.",
    "{name} is the 'before' photo for hitting rock bottom.",
    "{name} is a cry for help nobody bothered to answer.",
    "{name}'s family group chat has them on permanent mute.",
    "{name} is the friend everyone keeps around to feel better about their own life.",
    "{name} got left on read by a literal bot.",
    "{name} couldn't get a pity invite to their own funeral.",
    "{name}'s glow-up took a wrong exit and was never seen again.",
    "{name} got ghosted by a group they weren't even in.",
    "{name}'s entire vibe is 'last one picked, then un-picked.'",

    # --- hygiene / bodily (crude) ---
    "{name} smells like a gym sock that gave up on its dreams.",
    "{name}'s breath could strip the paint off a battleship.",
    "{name} sweats gravy.",
    "{name}'s farts have been formally classified as a chemical weapon.",
    "{name} showers so rarely the soap filed a missing persons report.",
    "{name} has the hygiene of a porta-potty at hour eight of a festival.",
    "{name} could clear a room with a single exhale.",
    "{name}'s funk has its own zip code.",

    # --- appearance (generic) ---
    "{name} has a face that makes onions cry back.",
    "{name}'s baby pictures are handed out as free birth control.",
    "{name} looks like a thumb in a wig.",
    "{name} looks like they were assembled from leftover parts in a hurry.",
    "{name} has the face you'd describe to a sketch artist as 'just regret.'",
    "{name} ruins group photos retroactively.",

    # --- personality / annoying ---
    "{name} has the charisma of a damp customs form.",
    "{name} talks so much their own ears filed for divorce.",
    "{name} is a car alarm at 3 a.m. in human form.",
    "{name} brings the room's energy down like a dropped Wi-Fi signal.",
    "{name} is a group project's weakest link wearing the leadership lanyard.",
    "{name}'s hot takes come out lukewarm and slightly damp.",
    "{name} is so boring their own thoughts wander off mid-sentence.",
    "{name} could bore the paint right off a wall.",
    "{name} is a podcast nobody subscribed to and somehow can't unsubscribe from.",
    "{name} is the reason the mute button got a software upgrade.",

    # --- Big Brother themed ---
    "{name} would get evicted week one and the houseguests would high-five on the way out.",
    "{name}'s game is so trash Zingbot would take one look and just leave.",
    "{name} would throw the comp, the season, and their own goodbye message.",
    "{name} would get backdoored so fast they'd tip the carpenter.",
    "{name} would flirt their whole showmance straight into a restraining order.",
    "{name}'s live feeds would be sponsored by Ambien.",
    "{name} would campaign so hard the jury would vote for an empty chair.",
    "{name} is the houseguest production keeps to make everyone else look like geniuses.",
    "{name}'s 'big move' would be moving back in with their parents post-eviction.",
    "{name} would get a unanimous eviction and a unanimous 'who?'",
    "{name} would form a final two with the camera and still get cut.",
    "{name} would self-evict and somehow still finish last.",
    "{name} would name their alliance, leak it, and then join the wrong side of it.",
    "{name}'s diary room sessions would need a laugh track and a defibrillator.",
    "{name} would lose HOH to a houseguest who already left the house.",
    "{name} plays the game like they only ever read the Wikipedia summary, wrong.",
    "{name} would get got by the dumbest person in the house and thank them for it.",
    "{name}'s social game has the warmth of a vending machine that ate your dollar.",
    "{name} would put up the wrong noms, win the wrong veto, and vote out themselves.",
    "{name} would betray an alliance that doesn't even know they exist.",

    # --- misc savage ---
    "{name} is the human version of a 'we need to talk' text.",
    "{name}'s comebacks arrive three weeks late, collect, and wrong.",
    "{name} could lose an argument to a voicemail greeting.",
    "{name} brings a butter knife to a gunfight and loses to the butter.",
    "{name} is a 1 percent battery in a dead zone, but make it a person.",
    "{name} is what autocorrect was invented to prevent.",
    "{name} is so forgettable their own echo refuses to repeat them.",
    "{name} could get blocked by a brick wall they built themselves.",
    "{name} is a 404 with a pulse and a parking ticket.",
    "{name} has the staying power of a Snapchat and the charm of a pop-up ad.",
    "{name} is the season finale nobody renewed and everybody forgot.",
    "{name} lost a staring contest to a sleeping cat.",
    "{name} is the reason 'are you a robot' tests exist, and they still fail them.",
    "{name} is the typo evolution forgot to spellcheck.",

    # --- extra batch ---
    "{name} hasn't been ridden since their tricycle.",
    "{name} got a 'maybe' from an inflatable.",
    "{name}'s headboard has never once filed a noise complaint, and that's the tragedy.",
    "{name} brings a permission slip to the bedroom and it comes back denied.",
    "{name} edges their entire dating life directly into the void.",
    "{name} thinks 'spit or swallow' is a question about sunflower seeds.",
    "{name}'s pickup line worked once, on a scam bot, and even it left.",
    "{name} has the bedroom presence of a fire extinguisher: behind glass, never used.",
    "{name} got blue-balled by a horoscope.",
    "{name}'s 'u up?' texts go straight to a fax machine.",
    "{name} couldn't get to second base with a season pass.",
    "{name}'s Tinder bio is just the word 'please.'",
    "{name} is what you get when you let the intern finish building a person.",
    "{name} is a wet fart in a quiet elevator.",
    "{name} is a group photo's collective flinch.",
    "{name} has the backbone of a wet receipt.",
    "{name} is proof you can fail upward into a brick wall.",
    "{name} is a hot mess, minus the hot.",
    "{name} thinks a thesaurus is a dinosaur.",
    "{name} tried to screenshot a smell.",
    "{name} got outsmarted by a child-proof cap.",
    "{name} put 'breathing' on their resume under special skills.",
    "{name} would get evicted and ask to stay for the catering.",
    "{name} would lose the veto to a competition nobody else entered.",
    "{name}'s gameplay is a hostage situation for the live feeders.",
    "{name} would blow up their own game and call it a showmance.",
    "{name} would get a pity vote and act like they swept the whole season.",
    "{name} would trust the wrong person so hard production would have to step in.",
    "{name} is a sad desk lunch in human form.",
    "{name}'s rock bottom installed a basement.",
    "{name} got un-invited from a wedding they were officiating.",
    "{name} is the human version of a 'continue watching' nobody continues.",
    "{name} could disappoint a Magic 8-Ball into early retirement.",
    "{name} is the reason 'we should hang out sometime' was invented as a goodbye.",
    "{name} has the emotional depth of a kiddie pool in a drought.",
    "{name} is a clearance bin nobody bothered to dig through.",
]


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

    @app_commands.command(
        name="zing",
        description="Let Zingbot roast someone. Pick a target, or leave it blank for a random victim.",
    )
    @app_commands.describe(target="Who to roast. Leave empty and Zingbot picks a random victim.")
    async def zing(self, interaction: discord.Interaction, target: Optional[discord.Member] = None):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Zingbot only works inside a server.", ephemeral=True)
            return

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
