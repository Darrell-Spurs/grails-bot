import os, sys

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
import requests
from utils.spotify_utils import get_access_token
from utils.helpers import get_filtered_albums

class AlbumCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def album(self, ctx, *, artist_name):
        token = get_access_token()

        filtered_albums, artist_name_corrected, _ = await get_filtered_albums("album", artist_name, ctx, token)

        print(len(filtered_albums), "albums after filtering")

        unique_album_names = [album["name"] for album in filtered_albums]

        reply = f"💿 Albums by **{artist_name_corrected}**:\n"
        reply += "\n".join(f"- {name}" for name in unique_album_names)  # limit output

        await ctx.send(reply)

async def setup(bot):
    await bot.add_cog(AlbumCog(bot))
