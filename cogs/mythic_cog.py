"""Public read-only view of a mythic's remaining copies.

Distinct from the admin `.mythicstatus`, which takes a slash-separated
"song / album / artist" string and is shaped for debugging. This one is
autocompleted end to end: pick the artist, then the song list narrows to that
artist's songs, so a player never has to type an exact title.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging

import discord
from discord import app_commands
from discord.ext import commands

import db
from utils import aesthetics
from utils.command_types import slash_only
from utils.errors import report_unhandled

log = logging.getLogger("grails.mythic")


async def _artist_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    try:
        artists = db.get_all_artists()
    except Exception:
        log.exception("mythiccheck artist autocomplete failed")
        return []
    return [app_commands.Choice(name=a, value=a) for a in artists if cur in a.lower()][:25]


async def _song_autocomplete(interaction: discord.Interaction, current: str):
    """Songs narrowed to whichever artist has already been chosen.

    The value is the song id, so the command never has to re-resolve a title
    that two artists might share.
    """
    artist = getattr(interaction.namespace, "artist", None)
    if not artist:
        return [app_commands.Choice(name="Pick an artist first", value="none")]

    cur = (current or "").lower()
    try:
        rows = db.get_songs_with_ids_by_artist(artist)
    except Exception:
        log.exception("mythiccheck song autocomplete failed")
        return []

    # (song_id, name, rarity, album_name, track_count, image, album_id); a song
    # on several albums repeats, so collapse by id.
    seen, choices = set(), []
    for song_id, name, rarity, *_rest in rows:
        if song_id in seen or cur not in name.lower():
            continue
        seen.add(song_id)
        choices.append(app_commands.Choice(name=f"{name} ({rarity})"[:100], value=song_id))
        if len(choices) == 25:
            break
    return choices or [app_commands.Choice(name="No songs match", value="none")]


class MythicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="mythiccheck",
        description="How many mythic copies of a song are left")
    @slash_only()
    @app_commands.describe(artist="The artist", song="The song (pick the artist first)")
    @app_commands.autocomplete(artist=_artist_autocomplete, song=_song_autocomplete)
    async def mythiccheck(self, ctx, artist: str, song: str):
        await ctx.defer()

        if song == "none":
            await ctx.send("Pick a song from the autocomplete list.")
            return

        resolved = await self.bot.loop.run_in_executor(
            None, self._resolve, artist, song)
        if resolved is None:
            await ctx.send(f"🔍 Could not find that song under **{artist}**.")
            return

        song_id, song_name, artist_name, rarity, album_name, album_image = resolved
        info = await self.bot.loop.run_in_executor(None, db.get_mythic_copy_info, song_id)
        mine = await self.bot.loop.run_in_executor(
            None, db.get_user_mythic_copy_number, ctx.author.id, song_id)

        claimed, cap = info["current_copies"], info["max_copies"]
        filled = "●" * claimed + "○" * max(cap - claimed, 0)

        embed = discord.Embed(
            title=f"{aesthetics.card_emoji(rarity, 'mythic')} {song_name}",
            description=f"{artist_name}" + (f" · _{album_name}_" if album_name else ""),
            colour=discord.Colour(aesthetics.rarity_colour_int(rarity)),
        )
        embed.add_field(name="Copies claimed", value=f"`{filled}`  {claimed} / {cap}", inline=False)
        embed.add_field(
            name="Still available" if info["can_collect"] else "Sold out",
            value=(f"**{info['remaining_copies']}** left — the next claim will be "
                   f"**#{info['next_copy_number']}**")
            if info["can_collect"] else "Every copy has been claimed.",
            inline=False,
        )
        # Whether *you* hold one is safe to show; who else does is not disclosed.
        if mine:
            embed.add_field(name="You", value=f"You hold copy **#{mine}**", inline=False)

        if album_image:
            embed.set_thumbnail(url=album_image)
        embed.set_footer(text="Mythics are capped server-wide — once they are gone, they are gone.")

        await ctx.send(embed=embed)

    @staticmethod
    def _resolve(artist, song):
        """Turn (artist, song) into full song details.

        `song` is normally a song id from autocomplete; a typed title is matched
        against that artist's songs so the prefix-less path still works.
        """
        rows = db.get_songs_with_ids_by_artist(artist)
        if not rows:
            return None

        by_id = {r[0]: r for r in rows}
        row = by_id.get(song)
        if row is None:
            wanted = song.strip().lower()
            row = next((r for r in rows if r[1].lower() == wanted), None)
            if row is None:
                row = next((r for r in rows if wanted in r[1].lower()), None)
        if row is None:
            return None

        song_id, name, rarity, album_name, _tc, image, _aid = row
        # get_songs_with_ids_by_artist already carries the album art, so no
        # second lookup is needed.
        return song_id, name, artist, rarity, album_name, image

    @mythiccheck.error
    async def mythiccheck_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Use `/mythiccheck artist:<name> song:<name>`.")
            return
        await report_unhandled(log, ctx, error, command="mythiccheck")


async def setup(bot):
    await bot.add_cog(MythicCog(bot))
