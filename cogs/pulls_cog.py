"""Checking how many choice drops you have, and when the next one lands.

One command, /cooldown (.cooldown / .cd). There used to be a /pulls as well,
but it showed exactly the same embed under a second name.
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

    # Plain lines in the description, not fields, and live Discord countdowns
    # that tick in the client. The countdown tag renders its own "in", so the
    # line reads "Next in 2 minutes" without writing "in" here.
    if full:
        timers = "\nFully charged, drop now with `.c`!"
    else:
        timers = (f"\nNext {economy.discord_countdown(status['next_in'])}\n"
                  f"Full again {economy.discord_countdown(status['full_in'])}")

    embed = discord.Embed(
        title="Choice drops",
        description=f"`{economy.charge_bar(charges)}`  **{charges} / {cap}**\n{timers}",
        # Green once the stack is full, so "am I wasting regeneration?" is
        # answerable from the colour alone.
        colour=discord.Colour.green() if full else discord.Colour(aesthetics.ACCENT_COLOR_INT),
    )
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)

    embed.set_footer(text=f"Collect up to {economy.PULL_CAP} drops at a rate of one every {economy.PULL_REGEN_SECONDS // 60} minutes. Use .c to spend them.")
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

    @commands.hybrid_command(name="cooldown", aliases=["cd"],
                             description="When your next choice drop arrives")
    async def cooldown(self, ctx, user: discord.Member = None):
        """Check when your next choice drop arrives.

        Usage: /cooldown — or .cooldown / .cd
        """
        await self._show(ctx, user)

    @cooldown.error
    async def cooldown_error(self, ctx, error):
        await report_unhandled(log, ctx, error, command="cooldown")


async def setup(bot):
    await bot.add_cog(PullsCog(bot))
