import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
from utils.helpers import get_collection_by_artist, get_collection_by_rarity, get_collection_by_rarity_and_artist, get_collection_by_user
import discord
from discord.ext import commands
from discord.ext import menus


RARITIES = ["mythic", "vinyl", "sketch", "glitched", "default"]

class CollectionPageSource(menus.ListPageSource):
    def __init__(self, data, title):
        self.title = title
        super().__init__(data, per_page=20)

    async def format_page(self, menu, entries):
        embed = discord.Embed(title=self.title, color=discord.Color.teal())
        rarity_emojis = {
            "mythic": "💎",
            "vinyl": "💿",
            "sketch": "✏️",
            "glitched": "🧩",
            "default": "⚪️"
        }
        for name, artist, variant, album, image in entries:
            embed.add_field(
                name=f"{rarity_emojis[variant]} {name} - {artist}",
                value=f"\n *{album or 'Unknown'}*",
                inline=False
            )
        embed.set_footer(text=f"Page {menu.current_page + 1}/{self.get_max_pages()}")
        return embed

class CollectionMenu(menus.MenuPages):
    def __init__(self, source):
        super().__init__(source=source, clear_reactions_after=True, timeout=60)

class CollectionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="collection")
    async def collection(self, ctx, *args):
        user_id = str(ctx.author.id)
        rarity = None
        artist = None
        print(f"User {ctx.author.display_name} requested their collection with args: {args}")

        for arg in args:
            if arg.lower() in RARITIES:
                print(f"Found rarity filter: {arg.lower()}")
                rarity = arg.lower()
            else:
                artist = " ".join([a for a in args if a.lower() not in RARITIES])
                break

        if rarity and artist:
            print("rarity and artist filters found")
            results = get_collection_by_rarity_and_artist(user_id, rarity, artist)
            title = f"📖 {ctx.author.display_name}'s {artist.title()} {rarity.title()} Collection"
            print(f"Filtered collection by rarity '{rarity}' and artist '{artist}' for user {user_id}")
        
        elif rarity:
            results = get_collection_by_rarity(user_id, rarity)
            title = f"📖 {ctx.author.display_name}'s {rarity.title()} Collection"
            print(f"Filtered collection by rarity '{rarity}' for user {user_id}")
        
        elif artist:
            print(f"Filtering collection by artist: {artist}")
            results = get_collection_by_artist(user_id, artist)
            title = f"📖 {ctx.author.display_name}'s {artist.title()} Collection"
            print(f"Filtered collection by artist '{artist.title()}' for user {user_id}")
        
        else:
            results = get_collection_by_user(user_id)
            title = f"📖 {ctx.author.display_name}'s Collection"
            print(f"Retrieved full collection for user {user_id}")

        if not results:
            await ctx.send("📭 No cards found in your collection with those filters.")
            return

        menu = CollectionMenu(source=CollectionPageSource(results, title))
        await menu.start(ctx)

    @collection.error
    async def collection_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Cool down! Try again in {round(error.retry_after)}s.")
            return

async def setup(bot):
    await bot.add_cog(CollectionCog(bot))
