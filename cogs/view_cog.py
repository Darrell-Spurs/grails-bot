import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from utils.aesthetics import VARIANT_LABEL, VARIANTS
from utils.card_art import ART_FILENAME, card_art_file
from utils.collection_ui import CardView, build_card_embed
from utils.command_types import slash_only

log = logging.getLogger("grails.view")

# How much has to be typed before the card list appears.
MIN_AUTOCOMPLETE_CHARS = 2


def _filters(interaction):
    """(variant, artist) as chosen so far on this invocation."""
    ns = interaction.namespace
    variant = (getattr(ns, "variant", None) or "").lower() or None
    artist = getattr(ns, "artist", None) or None
    return (variant if variant in VARIANTS else None), artist


async def _variant_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    return [app_commands.Choice(name=VARIANT_LABEL.get(v, v), value=v)
            for v in VARIANTS
            if cur in v.lower() or cur in VARIANT_LABEL.get(v, v).lower()][:25]


async def _artist_autocomplete(interaction: discord.Interaction, current: str):
    """Artists the viewed collection actually contains, not the whole catalogue."""
    target = getattr(interaction.namespace, "user", None)
    owner_id = target.id if target else interaction.user.id
    try:
        artists = db.get_user_collection_artists(owner_id)
    except Exception:
        log.exception("view artist autocomplete failed")
        return []
    cur = (current or "").lower()
    return [app_commands.Choice(name=a[:100], value=a[:100])
            for a in artists if cur in a.lower()][:25]


async def _card_autocomplete(interaction: discord.Interaction, current: str):
    """Cards belonging to whoever the command is pointed at.

    The value is a collections.id -- the same identifier /trade autocompletes on
    -- so one addressing scheme covers viewing, trading and pinning.
    """
    query = (current or "").strip()
    variant, artist = _filters(interaction)

    # The two-character rule exists because an unfiltered collection is hundreds
    # of cards and an arbitrary 25 of them tell you nothing. A variant or artist
    # filter has already cut it down, so the list is worth showing immediately.
    if not (variant or artist) and len(query) < MIN_AUTOCOMPLETE_CHARS:
        return []

    owner_id = (interaction.namespace.user.id
                if getattr(interaction.namespace, "user", None) else interaction.user.id)
    try:
        cards = db.search_user_cards(owner_id, query=query, limit=25,
                                     variant=variant, artist=artist)
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
        description="View a card. Defaults to your most recent pull.",
    )
    @slash_only()
    @app_commands.describe(
        user="Whose card to look at (defaults to you)",
        variant="Only cards of this variant",
        artist="Only cards by this artist",
        item="The card to show",
    )
    @app_commands.autocomplete(item=_card_autocomplete, variant=_variant_autocomplete,
                               artist=_artist_autocomplete)
    async def view(self, ctx, user: Optional[discord.User] = None,
                   variant: Optional[str] = None, artist: Optional[str] = None,
                   item: Optional[str] = None):
        owner = user or ctx.author
        owner_name = owner.display_name

        card, note = self._resolve(owner.id, item, variant=variant, artist=artist)
        if note:
            await ctx.send(note)
            return

        # Rendering is CPU-bound and downloads a cover, so it goes to a worker.
        art = await asyncio.to_thread(card_art_file, card)
        embed = build_card_embed(
            card, owner_name,
            copy_number=db.get_card_copy_number(card["collection_id"]),
            owners=db.count_card_owners(card["song_id"]),
            pinned=self._is_pinned(owner.id, card["collection_id"]),
            art_filename=ART_FILENAME if art else None,
            owner_icon=owner.display_avatar.url,
        )
        view = CardView(ctx.author.id, card, owner_name, owner_id=str(owner.id),
                        owner_icon=owner.display_avatar.url)
        kwargs = {"embed": embed, "view": view}
        if art:
            kwargs["file"] = art
        view.message = await ctx.send(**kwargs)

    def _resolve(self, owner_id, item, *, variant=None, artist=None):
        """Turn the `item` argument into one card, or an explanation.

        Accepts a collection id (what slash autocomplete sends) or free text
        (what prefix users type), so `/view` and `.view` resolve identically.

        With no item the filters decide what "latest" means: `/view
        variant:mythic` should show the newest mythic, not the newest card
        that happens not to be one.
        """
        variant = (variant or "").lower() or None
        if variant and variant not in VARIANTS:
            return None, ("\u274c **" + variant + "** is not a variant. Pick one of: "
                          + ", ".join(f"`{v}`" for v in VARIANTS))

        if not item:
            if variant or artist:
                # Highest collection id is the most recent: the column is an
                # identity, so it orders by insertion without needing a
                # timestamp that ties during a fast run of pulls.
                matches = db.search_user_cards(owner_id, limit=1000,
                                               variant=variant, artist=artist)
                if not matches:
                    return None, "\U0001F4ED No cards match those filters."
                return max(matches, key=lambda c: c["collection_id"]), None

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

        matches = db.search_user_cards(owner_id, query=item, limit=10,
                                       variant=variant, artist=artist)
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
