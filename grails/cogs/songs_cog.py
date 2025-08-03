import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
import requests

from utils.spotify_utils import get_access_token
from utils.helpers import get_songs_from_album

class SongsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def song(self, ctx, *, artist_name):
        token = get_access_token()

        songs = await get_songs_from_album(artist_name, ctx, token)

        reply = f"🎵 Songs by **{artist_name}**:\n"
        reply += "\n".join(f"- {name}" for name in songs)  # limit output

        await ctx.send(reply)

async def setup(bot):
    await bot.add_cog(SongsCog(bot))
