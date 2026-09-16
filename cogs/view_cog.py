import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from utils.collection_ui import CardView, build_card_embed
from utils.command_types import slash_only

log = logging.getLogger("grails.view")


async def _card_autocomplete(interaction: discord.Interaction, current: str):
    """Cards belonging to whoever the command is pointed at.

    The value is a collections.id -- the same identifier /trade autocompletes on
    -- so one addressing scheme covers viewing, trading and pinning.
    """
    target = getattr(interaction.namespace, "user", None)
    owner_id = target.id if target else interaction.user.id
    try:
        cards = db.search_user_cards(owner_id, query=current or "", limit=25)
    except Exception:
        log.exception("view autocomplete failed")
        return []
    return [
        app_commands.Choice(
            name=f"{c['song_name']} by {c['artist']} ({c['rarity']})"[:100],
            value=str(c["collection_id"]),
        )
        for c in cards
    ]


class ViewCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="view",
        description="View one card. Defaults to your most recent pull.",
    )
    @slash_only()
    @app_commands.describe(
        item="The card to show (pick from autocomplete, or type part of a song name)",
        user="Whose card to look at (defaults to you)",
    )
    @app_commands.autocomplete(item=_card_autocomplete)
    async def view(self, ctx, item: Optional[str] = None, user: Optional[discord.User] = None):
        owner = user or ctx.author
        owner_name = owner.display_name

        card, note = self._resolve(owner.id, item)
        if note:
            await ctx.send(note)
            return

        embed = build_card_embed(
            card, owner_name,
            copy_number=db.get_card_copy_number(card["collection_id"]),
            owners=db.count_card_owners(card["song_id"]),
            pinned=self._is_pinned(owner.id, card["collection_id"]),
        )
        view = CardView(ctx.author.id, card, owner_name, owner_id=str(owner.id))
        view.message = await ctx.send(embed=embed, view=view)

    def _resolve(self, owner_id, item):
        """Turn the `item` argument into one card, or an explanation.

        Accepts a collection id (what slash autocomplete sends) or free text
        (what prefix users type), so `/view` and `.view` resolve identically.
        """
        if not item:
            card = db.get_latest_card(owner_id)
            if not card:
                return None, "\U0001F4ED No cards collected yet."
            return card, None

        item = item.strip()
        if item.isdigit():
            card = db.get_card(int(item))
            # An id from another player's collection is not an error -- /view
            # supports looking at other people -- but it must belong to the
            # person actually being viewed.
            if card and str(card["user_id"]) == str(owner_id):
                return card, None

        matches = db.search_user_cards(owner_id, query=item, limit=10)
        if not matches:
            return None, f"\U0001F50D No card matching **{item}** in that collection."
        if len(matches) == 1:
            return matches[0], None

        listing = "\n".join(
            f"`{m['collection_id']}` **{m['song_name']}** by **{m['artist']}** ({m['rarity']})"
            for m in matches[:10]
        )
        return None, (f"Several cards match **{item}** — run `.view <id>` with one of these:\n{listing}")

    @staticmethod
    def _is_pinned(user_id, collection_id):
        profile = db.get_user_profile(user_id)
        return bool(profile and profile["pinned_collection_id"] == collection_id)


async def setup(bot):
    await bot.add_cog(ViewCog(bot))
