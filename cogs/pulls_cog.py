"""Checking how many choice drops you have, and when the next one lands.

Two commands for one answer, because players reach for two different words:
"how many pulls do I have" and "how long is my cooldown". Discord does not
register a hybrid command's aliases as slash commands, so /pulls and /cooldown
have to be declared separately; both delegate to the same builder, so there is
only one place the status embed is defined.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import db
from utils import aesthetics, economy
from utils.errors import report_unhandled

log = logging.getLogger("grails.pulls")

ADMIN_ROLE = "grails-admin"


def build_status_embed(user, status):
    """The drop status for `user`, or a nudge to register when there is none."""
    if status is None:
        return discord.Embed(
            title="Choice drops",
            description="You need an account first — run **/register** to start collecting.",
            colour=discord.Colour(aesthetics.ACCENT_COLOR_INT),
        )

    charges, cap = status["charges"], status["cap"]
    full = charges >= cap

    embed = discord.Embed(
        title="Choice drops",
        description=f"`{economy.charge_bar(charges)}`  **{charges} / {cap}**",
        # Green once the stack is full, so "am I wasting regeneration?" is
        # answerable from the colour alone.
        colour=discord.Colour.green() if full else discord.Colour(aesthetics.ACCENT_COLOR_INT),
    )
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)

    if full:
        embed.add_field(
            name="Full",
            value="You are at the cap — drops stop accruing until you spend some.",
            inline=False,
        )
    else:
        embed.add_field(name="Next drop",
                        value=f"in **{economy.format_duration(status['next_in'])}**",
                        inline=True)
        embed.add_field(name="Full again",
                        value=f"in **{economy.format_duration(status['full_in'])}**",
                        inline=True)

    embed.set_footer(text=f"One drop every {economy.PULL_REGEN_SECONDS // 60} minutes, "
                          f"up to {economy.PULL_CAP}.  ·  spend them with .c")
    return embed


class PullsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _is_admin(self, ctx):
        """Checked at call time rather than with a decorator.

        The command itself is open to everyone; it is only the `user` argument
        that is privileged, and a command-level check cannot see arguments.
        """
        if await self.bot.is_owner(ctx.author):
            return True
        # In a DM there are no roles to inspect, and nobody to look up either.
        return any(r.name == ADMIN_ROLE for r in getattr(ctx.author, "roles", ()))

    async def _show(self, ctx, user=None):
        target = user or ctx.author

        if target.id != ctx.author.id and not await self._is_admin(ctx):
            # Ephemeral where it can be: a refusal is only of interest to the
            # person who asked, and it should not clutter the channel.
            await ctx.send("You can only check your own drops.", ephemeral=True)
            return

        status = await asyncio.to_thread(db.get_pull_status, target.id)
        await ctx.send(embed=build_status_embed(target, status))

    @commands.hybrid_command(name="pulls", aliases=["pull"],
                             description="How many choice drops you have stacked")
    async def pulls(self, ctx, user: discord.Member = None):
        """Check your choice drops.

        Usage: /pulls or .pulls
        """
        await self._show(ctx, user)

    @commands.hybrid_command(name="cooldown", aliases=["cd"],
                             description="When your next choice drop arrives")
    async def cooldown(self, ctx, user: discord.Member = None):
        """Check when your next choice drop arrives.

        Usage: /cooldown — or .cooldown / .cd
        """
        await self._show(ctx, user)

    @pulls.error
    async def pulls_error(self, ctx, error):
        await report_unhandled(log, ctx, error, command="pulls")

    @cooldown.error
    async def cooldown_error(self, ctx, error):
        await report_unhandled(log, ctx, error, command="cooldown")


async def setup(bot):
    await bot.add_cog(PullsCog(bot))
