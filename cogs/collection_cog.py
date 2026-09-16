import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from utils.collection_ui import VARIANTS, VARIANT_LABEL, CollectionView
from utils.command_types import slash_only

log = logging.getLogger("grails.collection")


async def _variant_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=VARIANT_LABEL[v], value=v)
        for v in VARIANTS if cur in v or cur in VARIANT_LABEL[v].lower()
    ][:25]


async def _own_artist_autocomplete(interaction: discord.Interaction, current: str):
    """Only artists the viewer actually owns something by -- offering the whole
    catalog would mostly suggest empty results."""
    target = getattr(interaction.namespace, "user", None)
    owner_id = target.id if target else interaction.user.id
    cur = (current or "").lower()
    try:
        artists = db.get_user_collection_artists(owner_id)
    except Exception:
        log.exception("collection artist autocomplete failed")
        return []
    return [app_commands.Choice(name=a, value=a) for a in artists if cur in a.lower()][:25]


class CollectionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="collection", aliases=["col"],
        description="Browse a user's song collection",
    )
    @slash_only()
    @app_commands.describe(
        variant="Filter by variant (mythic, signature, sketch...)",
        artist="Filter to one artist",
        user="Whose collection to browse (defaults to you)",
    )
    @app_commands.autocomplete(variant=_variant_autocomplete, artist=_own_artist_autocomplete)
    async def collection(self, ctx, variant: Optional[str] = None,
                         user: Optional[discord.User] = None, *,
                         artist: Optional[str] = None):
        # Prefix convenience: `.col taylor swift` should not require naming a
        # variant first, so a leading token that is not a variant is artist text.
        if variant and variant.lower() not in VARIANTS:
            artist = f"{variant} {artist}".strip() if artist else variant
            variant = None
        variant = variant.lower() if variant else None

        owner = user or ctx.author
        log.info("%s browsing %s's collection (variant=%s artist=%s)",
                 ctx.author.display_name, owner.display_name, variant, artist)

        view = CollectionView(
            invoker_id=ctx.author.id,
            owner_id=owner.id,
            owner_name=owner.display_name,
            variant=variant,
            artist=artist,
        )

        if not view.total:
            await ctx.send("\U0001F4ED No cards found with those filters.")
            return

        view.message = await ctx.send(embed=view.render(), view=view)


async def setup(bot):
    await bot.add_cog(CollectionCog(bot))
