import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from utils.collection_ui import (
    CardView, CollectionView, build_card_embed, card_emoji, rarity_emoji,
    rarity_colour,
)
from utils.command_types import slash_only
from utils import aesthetics

log = logging.getLogger("grails.profile")

BAR_WIDTH = 10


def _bar(done, total, width=BAR_WIDTH):
    if not total:
        return "░" * width
    filled = max(0, min(width, round(width * done / total)))
    return "▓" * filled + "░" * (width - filled)


class ProfileView(discord.ui.View):
    """Profile actions. Setting a pin or a favorite is owner-only; anyone
    looking at the profile can still jump to the collection."""

    def __init__(self, invoker_id, owner_id, owner_name, pinned_card, reopen=None,
                 owner_icon=None):
        super().__init__(timeout=180)
        self.invoker_id = int(invoker_id)
        self.owner_id = str(owner_id)
        self.owner_name = owner_name
        self.owner_icon = owner_icon
        self.pinned_card = pinned_card
        self.message = None
        # Rebuilds this profile from scratch. Passed to the views this one
        # opens so their Back button lands on a *fresh* profile -- pinning a
        # card and coming back should show the new pin, not a stale snapshot.
        self.reopen = reopen

        if str(invoker_id) != self.owner_id:
            self.remove_item(self.set_favorite)
        if pinned_card is None:
            self.remove_item(self.open_pinned)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "That menu belongs to someone else — run `/profile` yourself.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="View pinned card", emoji="\U0001F4CC",
                       style=discord.ButtonStyle.primary, row=0)
    async def open_pinned(self, interaction, button):
        card = self.pinned_card
        view = CardView(self.invoker_id, card, self.owner_name, owner_id=self.owner_id,
                        back_to=self.reopen, owner_icon=self.owner_icon)
        view.message = self.message
        embed = build_card_embed(
            card, self.owner_name,
            copy_number=db.get_card_copy_number(card["collection_id"]),
            owners=db.count_card_owners(card["song_id"]),
            pinned=True,
        )
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Collection", style=discord.ButtonStyle.secondary, row=0)
    async def open_collection(self, interaction, button):
        view = CollectionView(self.invoker_id, self.owner_id, self.owner_name,
                              back_to=self.reopen, owner_icon=self.owner_icon)
        view.message = self.message
        if not view.total:
            await interaction.response.send_message("That collection is empty.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=view.render(), view=view)

    @discord.ui.button(label="Set favorite artist", emoji="❤",
                       style=discord.ButtonStyle.secondary, row=0)
    async def set_favorite(self, interaction, button):
        artists = db.get_user_collection_artists(self.owner_id)
        if not artists:
            await interaction.response.send_message(
                "Collect something first — favorites are picked from artists you own.",
                ephemeral=True)
            return

        # A select is capped at 25 options; the list is the user's own artists,
        # which is short in practice, but truncate rather than error.
        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(
            placeholder="Pick your favorite artist…",
            options=[discord.SelectOption(label=a[:100], value=a[:100]) for a in artists[:25]],
        )

        async def on_pick(inner):
            db.set_favorite_artist(self.owner_id, select.values[0])
            await inner.response.edit_message(
                content=f"❤ Favorite artist set to **{select.values[0]}**. "
                        f"Run `/profile` again to see it.",
                view=None,
            )

        select.callback = on_pick
        view.add_item(select)
        await interaction.response.send_message(view=view, ephemeral=True)


class ProfileCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _level(self, xp):
        """Reuse XPCog's curve rather than restating it, so the two can't drift."""
        xp_cog = self.bot.get_cog("XPCog")
        if xp_cog:
            level = xp_cog.get_level_from_xp(xp)
            floor = xp_cog.get_xp_for_level(level)
            ceiling = xp_cog.get_xp_for_level(level + 1)
            return level, floor, ceiling
        return 0, 0, max(xp, 1)

    def _build_profile(self, owner_id, owner_name, avatar_url, invoker_id):
        """Build the profile embed, or None when the user is not registered.

        Pulled out of the command so the Back buttons on the collection and
        the pinned card can rebuild the exact same view. Returning it rather
        than sending means one definition serves the first render and every
        return trip.
        """
        data = db.get_user_profile(owner_id)

        if not data:
            return None

        pinned = None
        if data["pinned_collection_id"]:
            # Resolved with an ownership check: a pinned card that was traded
            # away must not keep showing on the old owner's profile.
            pinned = db.get_card(data["pinned_collection_id"], user_id=owner_id)
            if pinned is None:
                db.clear_pinned_card(owner_id)

        level, floor, ceiling = self._level(data["xp"])
        into = data["xp"] - floor
        span = max(ceiling - floor, 1)

        colour = rarity_colour(pinned["rarity"]) if pinned else discord.Colour(0x5B4BDB)
        embed = discord.Embed(title=f"", colour=colour)
        embed.set_author(name=owner_name + "’s Profile", icon_url=avatar_url)

        # One line in the description
        if pinned:
            variant = (pinned["variant"] or "default").lower()
            extra = ""
            if variant == "mythic":
                copy_no = db.get_card_copy_number(pinned["collection_id"])
                if copy_no:
                    extra = f" #{copy_no}"   # matches how a collection row shows the copy
            embed.description = (f"\U0001F4CC {card_emoji(pinned['rarity'], variant)} "
                                 f"**{pinned['song_name']}**{extra} — {pinned['artist']}")
            if pinned.get("album_image"):
                embed.set_image(url=pinned["album_image"])
        else:
            embed.description = ("\U0001F4CC Nothing pinned — open a card and "
                                 "press Pin to profile.")

        embed.add_field(
            name=f"Level {level}",
            value=f"`{_bar(into, span)}` {data['xp']:,} XP",
            inline=False,
        )

        embed.add_field(
            name="Collection",
            value=(f"{aesthetics.variant_emoji('default')} **{data['unique_songs']}** songs, {aesthetics.variant_emoji('mythic')} **{data['mythics']}** mythics"),
            inline=True,
        )

        # Chosen vs earned: the pair is the interesting part of a profile.
        favorite = data["favorite_artist"]
        if favorite:
            owned, total = db.get_artist_completion(owner_id, favorite)
            embed.add_field(
                name="Favorite artist",
                value=f"**{favorite}**\n`{_bar(owned, total)}` {owned}/{total} collected",
                inline=True,
            )
        else:
            embed.add_field(name="Favorite artist", value="_Not set_", inline=True)

        if data["collecting_since"]:
            embed.set_footer(text=f"Collecting since {str(data['collecting_since'])[:10]}")

        return embed, pinned

    @commands.hybrid_command(
        name="profile",
        description="View a player's profile.",
    )
    @slash_only()
    @app_commands.describe(user="Whose profile to show (defaults to you)")
    async def profile(self, ctx, user: Optional[discord.User] = None):
        owner = user or ctx.author
        built = self._build_profile(owner.id, owner.display_name,
                                    owner.display_avatar.url, ctx.author.id)
        if built is None:
            who = "You are" if owner.id == ctx.author.id else f"{owner.display_name} is"
            await ctx.send(f"{who} not registered yet — run `.register` to start collecting.")
            return
        embed, pinned = built
        def reopen():
            """Rebuild the profile view for a Back button."""
            fresh = self._build_profile(owner.id, owner.display_name,
                                        owner.display_avatar.url, ctx.author.id)
            if fresh is None:
                return None
            new_embed, new_pinned = fresh
            back = ProfileView(ctx.author.id, owner.id, owner.display_name,
                               new_pinned, reopen=reopen,
                               owner_icon=owner.display_avatar.url)
            return new_embed, back

        view = ProfileView(ctx.author.id, owner.id, owner.display_name, pinned,
                           reopen=reopen, owner_icon=owner.display_avatar.url)
        view.message = await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(ProfileCog(bot))
