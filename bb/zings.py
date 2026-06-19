"""Zingbot — a comedic roast command for the server.

`/zing` lets you roast a specific member, or leave the target blank and Zingbot
picks a random (human) victim from the server.

The roasts are generic, template-style burns with a {name} slot, ranging from
PG-13 to hard-R: crude, profane, and full of innuendo, but deliberately NOT
sexually explicit, and never aimed at protected characteristics (race, gender,
orientation, religion, disability). Because they're generic, they don't target
anyone's real traits — it's just dumb fun.

Freshness: a shuffle-bag hands out every roast once before any repeats.

NOTE: random-target mode needs the Server Members privileged intent enabled
(both in the Discord Developer Portal and via intents.members = True).
"""
from __future__ import annotations

import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

ROASTS = [
    # --- intelligence ---
    "{name} has two brain cells and they're both fighting for third place.",
    "If {name} had another brain cell, it would die of loneliness.",
    "{name} is living proof that evolution can throw it in reverse.",
    "{name}'s elevator doesn't quite reach the top floor.",
    "The wheel is spinning but the hamster's been dead for years with {name}.",
    "{name} could trip over a cordless phone.",
    "{name} is about as sharp as a bowling ball.",
    "If ignorance is bliss, {name} must be the happiest soul alive.",
    "{name} googles things they said out loud thirty seconds ago.",
    "{name} read a book once. It didn't take.",
    "{name} brought a knife to a thumb war and still lost.",
    "{name} thinks 'quinoa' is just a typo for something.",
    "{name} couldn't pour water out of a boot with the directions on the heel.",
    "{name} is the reason shampoo bottles have instructions.",
    "{name} treats common sense like an optional software update they keep ignoring.",
    "{name} is a few fries short of a Happy Meal, and the toy's missing too.",
    "{name} couldn't find water if they fell out of a boat.",
    "{name} fact-checks gossip and still gets it wrong.",
    "{name} loses arguments to bots and takes it personally.",
    "{name} is what autocorrect was invented to fix.",

    # --- looks (generic, nothing protected) ---
    "{name} has a face for radio and a voice for silent films.",
    "{name} isn't ugly, just aggressively forgettable.",
    "{name} looks like they were drawn with the wrong hand.",
    "Somewhere a mirror is filing a restraining order against {name}.",
    "{name}'s baby photos were handed out as free birth control.",
    "{name} looks like the 'before' picture for absolutely everything.",
    "{name} could lose a staring contest with a portrait.",
    "{name} is the reason group photos get cropped.",

    # --- personality / annoying ---
    "Talking to {name} is like arguing with a GPS that's confidently wrong.",
    "{name} lights up a room — by leaving it.",
    "{name} is the reason the group chat finally learned about the mute button.",
    "If being annoying were a sport, {name} would finally medal.",
    "{name} has the charisma of a damp sock.",
    "{name} has the depth of a paper cut.",
    "{name}'s hot takes come out lukewarm and undercooked.",
    "{name} runs their mouth like it's training for a marathon their brain skipped.",
    "{name} is a karaoke night that empties the bar.",
    "{name} tells stories with a built-in snooze button.",
    "{name} is so boring their own reflection naps.",
    "Watching paint dry asked {name} to pick up the pace.",
    "{name}'s personality is a loading bar stuck at one percent.",
    "{name} could bore a glass of water.",

    # --- loser / failure ---
    "{name} peaked in a group project they didn't even contribute to.",
    "{name}'s greatest accomplishment is being somebody else's cautionary tale.",
    "{name} has the ambition of a screensaver.",
    "{name} fails so consistently it's almost a talent.",
    "{name} is living proof that 'you can do anything' was a marketing lie.",
    "{name} could lose at a game they invented with rules they wrote.",
    "{name} couldn't win an argument in an empty room.",
    "{name} couldn't organize a one-car funeral.",
    "{name} couldn't sell water in a desert, even with a coupon.",
    "{name} is a walking L with a pulse.",
    "{name} is the season finale nobody renewed.",
    "{name} fumbles wins they already had in the bag.",
    "{name} is a cautionary tale with a haircut.",
    "{name} is the reason 'skill issue' trends.",

    # --- social / no friends ---
    "{name}'s only friends are the ones the algorithm assigns.",
    "{name} throws a party and even the silence doesn't RSVP.",
    "{name}'s contact list is just delivery drivers.",
    "People leave {name} on read purely out of tradition.",
    "{name} could start a cult and still get zero sign-ups.",
    "{name} got friend-zoned by their own reflection.",
    "{name} could get blocked by a wall they built themselves.",
    "{name} is the collective sigh of every group chat.",

    # --- crude / hygiene (R) ---
    "{name} sweats so much their deodorant filed for hazard pay.",
    "{name} can clear a room faster than a fire alarm.",
    "{name}'s breath could knock a buzzard off a manure truck.",
    "{name} showers in cologne because the soap gave up.",
    "The only thing {name} kills is the vibe and a few houseplants.",
    "{name} is the human equivalent of stepping on a Lego.",
    "{name} brings the energy of a dead battery to every party.",
    "{name} is a horror movie where the call is coming from inside their own mouth.",

    # --- innuendo / love life (R, not explicit) ---
    "The only action {name} gets is a push notification.",
    "{name}'s love life has the same energy as a buffering screen.",
    "{name} couldn't get laid in a monkey brothel with a bag of bananas.",
    "{name}'s dating profile is just a formal apology.",
    "The closest {name} gets to a date is the one on a milk carton.",
    "{name} is so bad in bed even the pillow ghosts them.",
    "{name} has the rizz of a wet paper towel.",
    "{name}'s flirting is grounds for a noise complaint.",
    "{name}'s headboard has never once filed a complaint, and that's the problem.",
    "{name}'s 'u up?' texts go straight to voicemail.",
    "{name} has the seductive energy of a parking ticket.",
    "{name} thinks foreplay is asking permission to be disappointing.",
    "{name} got rejected by a 'this number is no longer in service.'",
    "{name} couldn't pull a hamstring, let alone a date.",
    "{name}'s pickup lines have a zero percent success rate and a hundred percent cringe rate.",
    "{name} flirts like they're reading the terms and conditions out loud.",
    "{name}'s 'Netflix and chill' is just Netflix and a sad, solo chill.",
    "{name} has the bedroom confidence of a fire drill.",
    "{name} thinks stamina is a brand of paper towel.",
    "{name} brings a participation ribbon and an apology note to the bedroom.",
    "{name}'s standards are in the basement and their results are in the sub-basement.",
    "{name} has been left on 'delivered' by life itself.",

    # --- Big Brother themed ---
    "{name} would be the first one out the door — production wouldn't even air the goodbye messages.",
    "{name} is such a floater the pool is getting jealous.",
    "{name} would put themselves on the block and call it strategy.",
    "{name}'s social game is so bad even the cameras look away.",
    "{name} would win the veto and use it on the wrong person.",
    "{name} would throw a comp they were already winning.",
    "{name} talks game like they've never actually seen the show.",
    "If {name} were a houseguest, the live feeds would buffer out of sheer boredom.",
    "{name} would get evicted unanimously and still call it a blindside.",
    "{name} would form an alliance of one and still get backdoored.",
    "{name}'s diary room sessions would be sponsored by NyQuil.",
    "{name} would campaign so hard the house would self-evict just to escape.",
    "{name} is the houseguest the edit forgets exists.",
    "{name} would call themselves a mastermind from the jury house.",
    "{name} couldn't win HOH if they were the only one playing.",
    "{name} would get got week one and thank the house for the experience.",
    "{name}'s 'biggest move' would be moving to the jury house.",
    "{name} would snore through the one HOH comp they could win.",
    "{name} would read the wrong name at eviction and still get booed.",
    "{name} would lock in a final two with someone who already cut them.",
    "{name}'s whole strategy is 'lay low,' which is just losing in slow motion.",
    "{name} would get a pity vote and act like they swept the season.",
    "{name} would campaign against themselves by accident.",
    "{name} is the houseguest the recap calls 'and others.'",
    "{name} would trust the wrong person so hard production would step in.",

    # --- savage general ---
    "{name} is the human equivalent of a typo.",
    "{name} is the answer to a question nobody asked.",
    "{name} is the 'are you still watching?' of human beings.",
    "{name} is the reason 'skip intro' exists.",
    "{name} is the plot twist nobody wanted.",
    "{name} is a cry for help with legs.",
    "{name} brings down the average just by being counted.",
    "{name} is the trailer that spoils its own bad movie.",
    "{name} is the human form of a dropped call.",
    "{name} is the 'loading...' that never finishes loading.",
    "{name} is the punchline that forgot its own joke.",
    "{name} has main-character energy in a deleted scene.",
    "{name} is the 'no signal' screen of conversations.",
    "{name} couldn't impress a Magic 8-Ball.",
    "{name} has the staying power of a Snapchat.",
    "{name} is the human version of a one percent phone with no charger.",
    "{name}'s comebacks arrive by mail, three weeks late, postage due.",
    "{name} is so forgettable their own echo refuses to repeat them.",
    "{name} couldn't win a popularity contest against a pop-up ad.",
    "{name} is proof that even autocorrect gives up sometimes.",
    "{name} brings a butter knife to every gunfight and loses to the butter.",
    "{name} is the typo in the group's autobiography.",
    "{name} is a software demo that crashes on the title slide.",
    "{name} has the aim of a stormtrooper and the luck of a black cat.",
    "{name} is the discount version of someone nobody liked to begin with.",
    "{name} is a walking 'my bad' with zero follow-through.",
    "{name} is the 'we need to talk' text in human form.",
    "{name} is the human embodiment of a participation trophy melting in the sun.",
    "{name} treats logic like a suggestion box they never open.",
    "{name} could disappoint a screensaver.",

    # --- cheap / money ---
    "{name} tips in compliments and 'exposure.'",
    "{name}'s wallet has more cobwebs than cash.",
    "{name} splits the bill down to the napkin.",
    "{name} reuses dental floss to save a buck.",
    "{name}'s idea of investing is hiding cash from themselves and forgetting where.",

    # --- terminally online ---
    "{name}'s entire personality is a repost.",
    "{name} has more screen time than the local multiplex.",
    "{name} replies 'this' to their own posts.",
    "{name} loses debates in the comments and then blocks the screenshot.",
    "{name} is so basic the algorithm gave up recommending them anything.",

    # --- more crude / profane (R) ---
    "{name} is the participation award of human beings, and even that's on backorder.",
    "{name} has the personality of unseasoned chicken.",
    "{name} is the reason the 'close door' button on elevators doesn't actually work — useless, but always pressing.",
    "{name} is a group project's weakest link wearing a leadership badge.",
    "{name} couldn't get a rise out of anyone, and that's both an insult and a diagnosis.",
    "{name}'s charisma comes with a recall notice.",
    "{name} is the reason warranties say 'void if used.'",
    "{name} is the 'press any key' when there is no any key.",
    "{name} runs their mouth with a controller that isn't plugged in.",
    "{name} is the human equivalent of a wet firework.",
    "{name} is a horror sequel nobody asked for and everybody skipped.",
    "{name} has the range of a flip phone.",
    "{name} is what the group means when they say 'seen, 9:41.'",
    "{name} brings a fork to the soup kitchen of life.",
    "{name} is the reason 'do not eat' is printed on things.",
    "{name} couldn't fumble their way out of a paper bag, and yes, they've tried.",
    "{name} is a dead battery in a world full of outlets.",
    "{name} is the human version of a 404 error.",
    "{name} talks a big game in a stadium that's been condemned.",
    "{name} is the reason the 'skip' button wears out first.",

    # --- extra batch ---
    "{name} couldn't win a coin toss they called twice.",
    "{name} has the spine of a chocolate eclair.",
    "{name} could lose a game of solitaire to themselves.",
    "{name} is what happens when confidence and competence never meet.",
    "{name} brings a spork to a knife fight and licks it.",
    "{name} peaked the day before they were born.",
    "{name} couldn't lead a horse to water it was already standing in.",
    "{name} has the survival instincts of a moth at a bug zapper.",
    "{name} is the human form of a Monday morning.",
    "{name} could get outvoted in a room by themselves.",
    "{name} is the warning label other people learn from.",
    "{name} would lose a rap battle to an out-of-office reply.",
    "{name} has the emotional depth of a kiddie pool in a drought.",
    "{name} is the friend people keep around to feel better about themselves.",
    "{name} couldn't pass a vibe check from a houseplant.",
    "{name} brings nothing to the table and then asks for a to-go box.",
    "{name} is the human version of stepping in something on brand-new shoes.",
    "{name} could fail an eye exam they were handed the answers to.",
    "{name} is the 'reply all' nobody wanted.",
    "{name}'s glow-up took a wrong turn and never made it back.",
    "{name} would get friend-zoned by a customer service chatbot.",
    "{name} is the reason 'this could have been an email' was invented.",
    "{name} would get evicted and ask to stay for the snacks.",
    "{name}'s jury speech would lose them votes they already had.",
    "{name} would win one comp all summer and put it on their resume.",
    "{name} is the human equivalent of a phone at three percent in a dead zone.",
]


class ZingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._bag: list[int] = []  # shuffle-bag: every roast used once before repeats

    def _next_roast(self) -> str:
        if not self._bag:
            self._bag = list(range(len(ROASTS)))
            random.shuffle(self._bag)
        return ROASTS[self._bag.pop()]

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

        roast = self._next_roast().format(name=target.display_name)
        embed = discord.Embed(
            title="🤖  ZING!",
            description=f"{target.mention}\n\n{roast}",
            color=0xE91E63,
        )
        embed.set_footer(text="Zingbot has spoken. 🔥")
        await interaction.response.send_message(embed=embed)
