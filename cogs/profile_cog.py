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
    RARITY_ORDER, CardView, CollectionView, build_card_embed, card_emoji, rarity_emoji,
    rarity_colour,
)
from utils.command_types import slash_only

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

    def __init__(self, invoker_id, owner_id, owner_name, pinned_card):
        super().__init__(timeout=180)
        self.invoker_id = int(invoker_id)
        self.owner_id = str(owner_id)
        self.owner_name = owner_name
        self.pinned_card = pinned_card
        self.message = None

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
        view = CardView(self.invoker_id, card, self.owner_name, owner_id=self.owner_id)
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
        view = CollectionView(self.invoker_id, self.owner_id, self.owner_name)
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

    @commands.hybrid_command(
        name="profile",
        description="View a player's profile.",
    )
    @slash_only()
    @app_commands.describe(user="Whose profile to show (defaults to you)")
    async def profile(self, ctx, user: Optional[discord.User] = None):
        owner = user or ctx.author
        data = db.get_user_profile(owner.id)

        if not data:
            who = "You are" if owner.id == ctx.author.id else f"{owner.display_name} is"
            await ctx.send(f"{who} not registered yet — run `.register` to start collecting.")
            return

        pinned = None
        if data["pinned_collection_id"]:
            # Resolved with an ownership check: a pinned card that was traded
            # away must not keep showing on the old owner's profile.
            pinned = db.get_card(data["pinned_collection_id"], user_id=owner.id)
            if pinned is None:
                db.clear_pinned_card(owner.id)

        level, floor, ceiling = self._level(data["xp"])
        into = data["xp"] - floor
        span = max(ceiling - floor, 1)

        colour = rarity_colour(pinned["rarity"]) if pinned else discord.Colour(0x5B4BDB)
        embed = discord.Embed(title=f"\U0001F3A7 {owner.display_name}", colour=colour)
        embed.set_thumbnail(url=owner.display_avatar.url)

        if pinned:
            variant = (pinned["variant"] or "default").lower()
            extra = ""
            if variant == "mythic":
                copy_no = db.get_card_copy_number(pinned["collection_id"])
                if copy_no:
                    extra = f" #{copy_no}"   # matches how a collection row shows the copy
            embed.add_field(
                name="\U0001F4CC Pinned card",
                value=(f"{card_emoji(pinned['rarity'], variant)} **{pinned['song_name']}**{extra} "
                       f"— {pinned['artist']}"),
                inline=False,
            )
            if pinned.get("album_image"):
                embed.set_image(url=pinned["album_image"])
        else:
            embed.add_field(
                name="\U0001F4CC Pinned card",
                value="_Nothing pinned — open a card and press Pin to profile._",
                inline=False,
            )

        embed.add_field(
            name=f"Level {level}",
            value=f"`{_bar(into, span)}` {data['xp']:,} XP\n{max(ceiling - data['xp'], 0):,} to next level",
            inline=False,
        )

        embed.add_field(
            name="Collection",
            value=(f"**{data['cards']}** cards · **{data['unique_songs']}** unique songs\n"
                   f"\U0001F48E **{data['mythics']}** mythics"),
            inline=True,
        )

        spread = " ".join(
            f"{rarity_emoji(r)}{data['rarity_counts'][r]}"
            for r in RARITY_ORDER if data["rarity_counts"].get(r)
        )
        embed.add_field(name="Rarity spread", value=spread or "—", inline=True)

        embed.add_field(
            name="Pulls available",
            value=f"\U0001F4BF {data['vinyl_count']} · \U0001F58B️ {data['sig_vinyl_count']}",
            inline=True,
        )

        # Chosen vs earned: the pair is the interesting part of a profile.
        favorite = data["favorite_artist"]
        if favorite:
            owned, total = db.get_artist_completion(owner.id, favorite)
            embed.add_field(
                name="❤ Favorite artist",
                value=f"**{favorite}**\n`{_bar(owned, total)}` {owned}/{total} collected",
                inline=True,
            )
        else:
            embed.add_field(name="❤ Favorite artist", value="_Not set_", inline=True)

        if data["top_artist"]:
            owned, total = db.get_artist_completion(owner.id, data["top_artist"])
            embed.add_field(
                name="\U0001F3C6 Most collected",
                value=f"**{data['top_artist']}**\n`{_bar(owned, total)}` {owned}/{total} collected",
                inline=True,
            )

        if data["collecting_since"]:
            embed.set_footer(text=f"Collecting since {str(data['collecting_since'])[:10]}")

        view = ProfileView(ctx.author.id, owner.id, owner.display_name, pinned)
        view.message = await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(ProfileCog(bot))
