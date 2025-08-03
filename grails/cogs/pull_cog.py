import os, sys

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
import requests
from utils.spotify_utils import get_access_token
from utils.helpers import get_random_song
from db import get_all_artists

class PullCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def pull(self, ctx):
        song, artist = get_random_song()
        reply = f"You pulled **{song}** by **{artist}**!"
        await ctx.send(reply)

async def setup(bot):
    await bot.add_cog(PullCog(bot))
