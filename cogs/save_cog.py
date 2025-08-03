import os, sys

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
from utils.spotify_utils import get_access_token
from utils.helpers import save_albums_to_db

class SaveCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def save_albums(self, ctx, *, artist_name):
        """Save all albums from an artist to the database"""
        await ctx.send(f"🔄 Saving albums by **{artist_name}** to database...")
        
        token = get_access_token()
        albums_saved, corrected_name = await save_albums_to_db(artist_name, ctx, token)
        
        if albums_saved > 0:
            await ctx.send(f"✅ Successfully saved **{albums_saved}** albums by **{corrected_name}** to the database!")
        else:
            await ctx.send(f"❌ No albums were saved for **{artist_name}**.")

async def setup(bot):
    await bot.add_cog(SaveCog(bot))
