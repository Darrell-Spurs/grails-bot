import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import menus
from utils.helpers import get_collection_by_artist, get_collection_by_rarity, get_collection_by_rarity_and_artist, get_collection_by_user
from db import (get_user_mythic_copy_number, get_collection_with_copy_numbers,
                get_collection_by_rarity_with_copy_numbers, get_collection_by_artist_with_copy_numbers,
                get_collection_by_rarity_and_artist_with_copy_numbers, get_all_artists)

log = logging.getLogger("grails.collection")

RARITIES = ["mythic", "sig_vinyl", "vinyl", "sketch", "glitched", "default"]


async def _rarity_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    return [app_commands.Choice(name=r.title(), value=r) for r in RARITIES if cur in r][:25]


async def _artist_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    artists = [a for a in get_all_artists() if cur in a.lower()]
    return [app_commands.Choice(name=a, value=a) for a in artists[:25]]

class CollectionPageSource(menus.ListPageSource):
    def __init__(self, data, title):
        self.title = title
        super().__init__(data, per_page=20)

    async def format_page(self, menu, entries):
        embed = discord.Embed(title=self.title, color=discord.Color.teal())
        rarity_emojis = {
            "mythic": "💎",
            "sig_vinyl": "🖋️",
            "vinyl": "📀",
            "sketch": "✏️",
            "glitched": "🧩",
            "default": "⚪️"
        }
        for entry in entries:
            # Handle both old format (5 elements) and new format (6 elements with copy_number)
            if len(entry) == 6:
                name, artist, variant, album, image, copy_number = entry
            else:
                name, artist, variant, album, image = entry
                copy_number = 1  # Default for non-mythic or when copy number isn't available

            # Format the name with copy number for mythics
            if variant == "mythic":
                display_name = f"{rarity_emojis[variant]} #{copy_number} {name} - {artist}"
            else:
                display_name = f"{rarity_emojis[variant]} {name} - {artist}"

            embed.add_field(
                name=display_name,
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

    @commands.hybrid_command(name="collection", aliases=["col"], description="View your song collection")
    @app_commands.describe(rarity="Filter by rarity", artist="Filter by artist")
    @app_commands.autocomplete(rarity=_rarity_autocomplete, artist=_artist_autocomplete)
    async def collection(self, ctx, rarity: Optional[str] = None, *, artist: Optional[str] = None):
        user_id = str(ctx.author.id)
        # Prefix convenience: if the first token isn't a real rarity, treat it as artist text
        # (so `.collection Taylor Swift` still works without naming a rarity).
        if rarity and rarity.lower() not in RARITIES:
            artist = f"{rarity} {artist}".strip() if artist else rarity
            rarity = None
        rarity = rarity.lower() if rarity else None
        log.info("%s requested collection (rarity=%s artist=%s)", ctx.author.display_name, rarity, artist)

        if rarity and artist:
            results = get_collection_by_rarity_and_artist_with_copy_numbers(user_id, rarity, artist)
            title = f"📖 {ctx.author.display_name}'s {artist.title()} {rarity.title()} Collection"

        elif rarity:
            results = get_collection_by_rarity_with_copy_numbers(user_id, rarity)
            title = f"📖 {ctx.author.display_name}'s {rarity.title()} Collection"

        elif artist:
            results = get_collection_by_artist_with_copy_numbers(user_id, artist)
            title = f"📖 {ctx.author.display_name}'s {artist.title()} Collection"

        else:
            results = get_collection_with_copy_numbers(user_id)
            title = f"📖 {ctx.author.display_name}'s Collection"

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
