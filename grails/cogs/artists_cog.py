import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
import requests

from utils.spotify_utils import get_access_token
from utils.helpers import get_all_artists

class ArtistsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def artists(self, ctx):

        artists = get_all_artists()

        reply = f"🎤 Available Artists:\n"
        reply += "\n".join(f"- {name}" for name in artists)

        await ctx.send(reply)

async def setup(bot):
    await bot.add_cog(ArtistsCog(bot))
