import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import logging
import random
import time
from collections import Counter

import discord
from discord import app_commands
from discord.ext import commands

from db import (add_song_to_collection, get_song_rarity, get_user_xp, add_user_xp,
                user_exists, register_user)
from utils.helpers import (get_random_song, get_random_album, make_battle_collage,
                           load_square_thumbnail, SOURPATCH_ID)
from utils.command_types import slash_only

log = logging.getLogger("grails.songbattle")

MAX_PLAYERS = 5
MIN_PLAYERS = 2
MIN_ROUNDS = 1
MAX_ROUNDS = 7
DEFAULT_ROUNDS = 3
JOIN_EMOJI = "🎮"
START_EMOJI = "✅"
JOIN_TIMEOUT = 45       # seconds to collect players
VOTE_TIMEOUT = 30       # seconds to collect votes (ends early once every participant has voted)
VOTE_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

# Same XP economy as a normal .choice "default" pull, so a battle pull is worth what a
# real pull would be worth. Duplicated from choice_cog.py's rarity_multiplier rather than
# importing across cogs, to keep this cog independently loadable.
RARITY_MULTIPLIER = {
    "ultimate": 3, "legendary": 2.5, "elite": 2, "unique": 1.5, "basic": 1
}
BASE_PULL_XP = 30
WINNER_BONUS_XP = 1000


def _pull_for_slot():
    """Runs entirely off the event loop: pick a random song/album, look up its rarity,
    and pre-fetch+cache its artwork thumbnail so later canvas redraws don't re-download it."""
    song, song_id, artist = get_random_song()
    album = get_random_album(song_id)
    rarity = None if song_id == SOURPATCH_ID else get_song_rarity(song_id)
    thumb = load_square_thumbnail(album[2])
    return song, song_id, artist, album, rarity, thumb


class SongBattleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # channel_id -> session dict. Also doubles as the "a battle is already running
        # in this channel" guard (present from lobby start through end of voting).
        self.active_battles = {}

    def _end_battle(self, channel_id):
        self.active_battles.pop(channel_id, None)

    @commands.hybrid_command(name="songbattle", aliases=["sb"],
                             description="Start a multiplayer song-pull battle (2-5 players)")
    @slash_only()
    @app_commands.describe(rounds=f"First to how many round-wins takes the match? ({MIN_ROUNDS}-{MAX_ROUNDS}, default {DEFAULT_ROUNDS})")
    async def songbattle(self, ctx, rounds: app_commands.Range[int, MIN_ROUNDS, MAX_ROUNDS] = DEFAULT_ROUNDS):
        """Start a song battle. Players react to join, pull order is randomized, then each
        player uses `.b`/`/b` on their turn to reveal their pull, and everyone votes at the end
        of each round. First to `rounds` round-wins takes the match."""
        if ctx.channel.id in self.active_battles:
            await ctx.send("❌ A song battle is already running in this channel!")
            return
        await self._run_battle(ctx, rounds)

    @songbattle.error
    async def songbattle_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ **Invalid rounds!** Choose a whole number from {MIN_ROUNDS} to {MAX_ROUNDS}.")
        else:
            raise error

    @commands.command(name="battle", aliases=["b"], description="Pull your song when it's your turn in a Song Battle")
    async def battle_pull(self, ctx):
        """Reveal your pull in the active Song Battle in this channel, if it's your turn."""
        session = self.active_battles.get(ctx.channel.id)
        if not session or session.get("phase") != "reveal":
            await ctx.send("❌ There's no song battle waiting for a pull in this channel! "
                           "Start one with `/songbattle`.", ephemeral=True)
            return

        slots = session["slots"]
        turn = session["turn"]
        current = slots[turn]["member"]

        if ctx.author.id != current.id:
            await ctx.send(f"❌ It's not your turn! Waiting on **{current.display_name}** to pull.",
                           ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        try:
            await self._reveal_pull(slots[turn])
            session["turn"] += 1
            pulled_song = slots[turn]["song"]
            pulled_artist = slots[turn]["artist"]
            is_last = session["turn"] >= len(slots)

            if not is_last:
                round_title = f"Song Battle — Round {session['round_num']}"
                next_player = slots[session["turn"]]["member"]
                await self._update_battle_canvas(
                    session["msg"], slots,
                    title=round_title,
                    description=f"**{next_player.display_name}**'s turn to pull! Use `.b` or `/b`."
                )
            # On the last pull, skip editing the old (possibly scrolled-past) message --
            # _finish_round resends the fully-revealed picture fresh instead.

            await ctx.send(f"🎟️ {ctx.author.display_name} pulled **{pulled_song}** by **{pulled_artist}**!", ephemeral=True)

            if is_last:
                await self._finish_round(session)
        except discord.HTTPException:
            log.exception("Song battle pull failed in channel %s", ctx.channel.id)
            await ctx.send("❌ Something went wrong. The battle has been cancelled.", ephemeral=True)
            self._end_battle(ctx.channel.id)

    async def _run_battle(self, ctx, rounds):
        self.active_battles[ctx.channel.id] = {"phase": "lobby"}

        try:
            players = await self._lobby_phase(ctx, rounds)
        except discord.HTTPException:
            log.exception("Song battle lobby failed in channel %s", ctx.channel.id)
            await ctx.send("❌ Something went wrong starting the song battle. Please try again.")
            self._end_battle(ctx.channel.id)
            return

        if players is None:
            self._end_battle(ctx.channel.id)
            return

        random.shuffle(players)  # decide turn order at random once; kept for the whole match

        # Reveal phase is now driven entirely by players invoking `.b`/`/b` on their turn.
        session = {
            "phase": "reveal",
            "ctx": ctx,
            "players": players,
            "slots": self._make_slots(players),
            "turn": 0,
            "round_num": 1,
            "rounds_target": rounds,
            "scores": {m.id: 0 for m in players},
            "msg": None,
        }

        try:
            session["msg"] = await self._send_battle_canvas(session)
        except discord.HTTPException:
            log.exception("Failed to send battle canvas in channel %s", ctx.channel.id)
            await ctx.send("❌ Something went wrong starting the song battle. Please try again.")
            self._end_battle(ctx.channel.id)
            return

        self.active_battles[ctx.channel.id] = session

    def _make_slots(self, players):
        return [
            {"member": m, "name": m.display_name, "thumb": None, "song": None,
             "artist": None, "rarity": None, "song_id": None, "album_id": None}
            for m in players
        ]

    # ---- lobby ----

    async def _lobby_phase(self, ctx, rounds):
        embed = discord.Embed(
            title="🎵 Song Battle!",
            description=(f"React with {JOIN_EMOJI} to join! ({MIN_PLAYERS}-{MAX_PLAYERS} players)\n"
                        f"**First to {rounds} win{'s' if rounds != 1 else ''} takes the match.**\n"
                        f"Starting in {JOIN_TIMEOUT}s or when full.\n"
                        f"*Host can react {START_EMOJI} to start early once {MIN_PLAYERS}+ have joined.*"),
            color=discord.Color.blurple()
        )
        embed.add_field(name="Players (1)", value=ctx.author.mention, inline=False)
        embed.set_footer(text=f"Started by {ctx.author.display_name}")
        msg = await ctx.send(embed=embed)
        await msg.add_reaction(JOIN_EMOJI)
        await msg.add_reaction(START_EMOJI)

        players = [ctx.author]
        self._ensure_registered(ctx.author)
        deadline = time.monotonic() + JOIN_TIMEOUT

        def check(reaction, user):
            if reaction.message.id != msg.id or user.bot:
                return False
            emoji = str(reaction.emoji)
            if emoji == JOIN_EMOJI:
                return True
            if emoji == START_EMOJI and user == ctx.author:
                return True
            return False

        while len(players) < MAX_PLAYERS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                reaction, user = await self.bot.wait_for('reaction_add', timeout=remaining, check=check)
            except asyncio.TimeoutError:
                break

            emoji = str(reaction.emoji)
            if emoji == START_EMOJI:
                if len(players) >= MIN_PLAYERS:
                    break  # host force-started the battle early
                continue  # not enough players yet; ignore the early-start request

            # emoji == JOIN_EMOJI
            if user in players:
                continue
            players.append(user)
            self._ensure_registered(user)
            embed.set_field_at(0, name=f"Players ({len(players)})",
                               value="\n".join(p.mention for p in players), inline=False)
            await msg.edit(embed=embed)

        try:
            await msg.clear_reactions()
        except discord.HTTPException:
            pass

        if len(players) < MIN_PLAYERS:
            embed.title = "❌ Song Battle Cancelled"
            embed.description = f"Not enough players joined (need at least {MIN_PLAYERS})."
            embed.color = discord.Color.red()
            await msg.edit(embed=embed)
            return None

        embed.title = "✅ Song Battle Starting!"
        embed.description = f"**{len(players)}** players ready. Deciding turn order..."
        embed.color = discord.Color.green()
        await msg.edit(embed=embed)
        return players

    def _ensure_registered(self, member):
        # Players who join via reaction never go through bot.before_invoke's auto-register,
        # so register them here to make sure their battle rewards actually land.
        if not user_exists(member.id):
            register_user(member.id, xp=0, username=member.name)

    # ---- battle canvas ----

    async def _send_battle_canvas(self, session, title=None, description=None):
        """Post the current battle picture as a brand-new message. Used both to start a round
        (blank slots) and to resend the fully-revealed picture fresh right before voting, so it
        isn't scrolled off-screen by all of that round's `.b` messages."""
        slots = session["slots"]
        buf = await asyncio.to_thread(make_battle_collage, slots, None, True)
        if title is None:
            title = f"🎶 Song Battle — Round {session['round_num']}"
        if description is None:
            first_player = slots[0]["member"]
            description = f"**{first_player.display_name}**'s turn to pull! Use `.b` or `/b`."
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        embed.set_image(url="attachment://battle.png")
        file = discord.File(buf, filename="battle.png")
        return await session["ctx"].send(embed=embed, file=file)

    async def _update_battle_canvas(self, msg, slots, title=None, description=None):
        buf = await asyncio.to_thread(make_battle_collage, slots, None, True)
        embed = msg.embeds[0]
        if title:
            embed.title = title
        if description:
            embed.description = description
        embed.set_image(url="attachment://battle.png")
        file = discord.File(buf, filename="battle.png")
        await msg.edit(embed=embed, attachments=[file])

    async def _reveal_pull(self, slot):
        song, song_id, artist, album, rarity, thumb = await asyncio.to_thread(_pull_for_slot)
        slot.update({
            "thumb": thumb, "song": song, "artist": artist, "rarity": rarity,
            "song_id": song_id, "album_id": album[0], "album_name": album[1],
        })
        log.info("Song battle reveal: %s -> %s by %s", slot["name"], song, artist)

    # ---- voting ----

    async def _voting_phase(self, msg, slots):
        emojis = VOTE_EMOJIS[:len(slots)]
        participant_ids = {s["member"].id for s in slots}
        options = "\n".join(f"{emojis[i]} {s['name']} — {s['song']} ({s['artist']})" for i, s in enumerate(slots))
        await self._update_battle_canvas(
            msg, slots,
            title="🗳️ Vote for the Best Pull!",
            description=(f"Players react with a number below! Voting ends in {VOTE_TIMEOUT}s "
                        f"\n\n{options}")
        )
        for e in emojis:
            await msg.add_reaction(e)

        votes = {}  # voter_id -> slot index. Only a voter's FIRST reaction counts -- check()
        # rejects anyone who has already voted, so a re-vote is ignored outright instead of
        # racing with the "everyone's voted" exit condition. This also means voting ends the
        # instant the last participant casts their first vote, with nothing left to wait on.
        deadline = time.monotonic() + VOTE_TIMEOUT

        def check(reaction, user):
            return (reaction.message.id == msg.id and str(reaction.emoji) in emojis
                   and user.id in participant_ids and user.id not in votes)

        while len(votes) < len(participant_ids):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                reaction, user = await self.bot.wait_for('reaction_add', timeout=remaining, check=check)
            except asyncio.TimeoutError:
                break
            votes[user.id] = emojis.index(str(reaction.emoji))

        try:
            await msg.clear_reactions()
        except discord.HTTPException:
            pass

        embed = msg.embeds[0]
        if not votes:
            embed.title = "🤷 No Votes Cast"
            embed.description = "Nobody voted — no winner this round. (Everyone still keeps their pull!)"
            await msg.edit(embed=embed)
            return []

        tally = Counter(votes.values())
        top_count = max(tally.values())
        winner_indices = [i for i, count in tally.items() if count == top_count]

        lines = [f"{emojis[i]} **{s['name']}** — {tally.get(i, 0)} vote(s)" for i, s in enumerate(slots)]
        if len(winner_indices) == 1:
            embed.title = f"🏆 {slots[winner_indices[0]]['name']} Wins the Round!"
        else:
            names = ", ".join(slots[i]['name'] for i in winner_indices)
            embed.title = f"🤝 Round Tied! ({names})"
        embed.description = "\n".join(lines)
        await msg.edit(embed=embed)
        return winner_indices

    # ---- round/match resolution ----

    async def _finish_round(self, session):
        """Called once every player has pulled in a round: resend the fully-revealed picture as
        a fresh message (so people can vote without scrolling back up), run the vote, grant that
        round's pulls, tally the score, and either end the match or start the next round with
        another fresh message/picture."""
        ctx = session["ctx"]
        slots = session["slots"]
        round_title = f"🎶 Song Battle — Round {session['round_num']}"

        session["msg"] = await self._send_battle_canvas(
            session,
            title=f"{round_title} — All Pulls Revealed!",
            description="Vote for the best pull below! 👇"
        )

        winner_indices = await self._voting_phase(session["msg"], slots)

        # Everyone keeps their pull every round, win or lose.
        await self._grant_round_pulls(ctx, slots)

        if len(winner_indices) == 1:
            winner_member = slots[winner_indices[0]]["member"]
            session["scores"][winner_member.id] = session["scores"].get(winner_member.id, 0) + 1
        # A round tie (or nobody voting) awards no round-win to anyone.

        await self._send_score_tally(session, slots, winner_indices)

        match_winner_ids = [uid for uid, score in session["scores"].items()
                            if score >= session["rounds_target"]]

        if match_winner_ids:
            await self._announce_match_winner(session, match_winner_ids)
            self._end_battle(ctx.channel.id)
            return

        session["round_num"] += 1
        session["turn"] = 0
        session["slots"] = self._make_slots(session["players"])
        session["msg"] = await self._send_battle_canvas(session)

    async def _send_score_tally(self, session, slots, winner_indices):
        players = session["players"]
        scores = session["scores"]
        score_lines = "\n".join(f"{p.display_name}: {scores.get(p.id, 0)}" for p in players)

        if len(winner_indices) == 1:
            winner = slots[winner_indices[0]]
            header = f"🏅 {winner['name']} won the round with {winner['song']} by {winner['artist']}!!"
        elif len(winner_indices) > 1:
            names = ", ".join(slots[i]['name'] for i in winner_indices)
            header = f"🤝 The round was tied between {names}!"
        else:
            header = "🤷 Nobody voted this round!"

        embed = discord.Embed(
            title=header,
            description=f"🔢 Current Score:\n{score_lines}",
            color=discord.Color.blurple()
        )
        await session["ctx"].send(embed=embed)

    async def _grant_round_pulls(self, ctx, slots):
        """Add every puller's song to their collection and grant XP for it -- independent of
        who won the round, matching the 'everyone keeps their pull' rule from a single-round battle."""
        xp_cog = self.bot.get_cog('XPCog')

        for slot in slots:
            member = slot["member"]
            if slot["song_id"] == SOURPATCH_ID:
                continue

            add_song_to_collection(member.id, slot["song_id"], slot["album_id"], variant="default")

            level_checkpoint = None
            if xp_cog:
                level_checkpoint = xp_cog.get_level_from_xp(get_user_xp(member.id))

            xp_gain = BASE_PULL_XP * RARITY_MULTIPLIER.get(slot["rarity"], 1)
            add_user_xp(member.id, xp_gain)

            if xp_cog and level_checkpoint is not None:
                await xp_cog.check_level_up(member.id, level_checkpoint, ctx.channel, member)

    async def _announce_match_winner(self, session, match_winner_ids):
        """Grant the match-winner XP bonus (split evenly among co-winners on a match tie) and
        post the final results embed."""
        ctx = session["ctx"]
        players = session["players"]
        scores = session["scores"]
        xp_cog = self.bot.get_cog('XPCog')

        for uid in match_winner_ids:
            member = discord.utils.get(players, id=uid)
            level_checkpoint = None
            if xp_cog:
                level_checkpoint = xp_cog.get_level_from_xp(get_user_xp(uid))
            add_user_xp(uid, WINNER_BONUS_XP)
            if xp_cog and level_checkpoint is not None:
                await xp_cog.check_level_up(uid, level_checkpoint, ctx.channel, member)

        ranked = sorted(players, key=lambda p: scores.get(p.id, 0), reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        scoreboard = "\n".join(
            f"{medals[i] if i < len(medals) else ''} {p.display_name} - {scores.get(p.id, 0)}".strip()
            for i, p in enumerate(ranked)
        )

        if len(match_winner_ids) == 1:
            winner = discord.utils.get(players, id=match_winner_ids[0])
            title = f"🏆 {winner.display_name} Wins the Match!"
        else:
            names = ", ".join(discord.utils.get(players, id=uid).display_name for uid in match_winner_ids)
            title = f"🤝 Match Tied Between {names}!"

        embed = discord.Embed(
            title=title,
            description=f"Final Score:\n{scoreboard}\n\n+{WINNER_BONUS_XP} bonus XP for the winner!",
            color=discord.Color.gold()
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(SongBattleCog(bot))
