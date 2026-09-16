import logging

import discord
from discord.ext import commands

from utils import aesthetics
from db import register_user, user_exists, unregister_user, add_sig_vinyl_count
from utils.command_types import slash_only

log = logging.getLogger("grails.register")

class RegisterCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(description="Register with the bot to start collecting")
    @slash_only()
    async def register(self, ctx):
        # Check if user is already registered
        if user_exists(ctx.author.id):
            await ctx.send(f"❌ **{ctx.author.display_name}**, you are already registered! You can start collecting XPs right away.")
            return

        register_user(ctx.author.id, xp=0, username=ctx.author.name)  # Register user in the database

        # Welcome gift. Granted after the account exists, since the grant is an
        # UPDATE against the users row.
        gift = 1
        try:
            add_sig_vinyl_count(ctx.author.id, gift)
        except Exception:
            # An account with no gift still beats a failed registration, so the
            # sign-up stands and the gift is logged for a manual top-up.
            log.exception("welcome signature vinyl failed for %s", ctx.author.id)
            gift = 0

        embed = discord.Embed(
            title="Welcome to Grails 🌟",
            description=f"Registered as **{ctx.author.display_name}** — you can start "
                        f"collecting **XP** right away.",
            colour=aesthetics.ACCENT_COLOR_INT,
        )
        if gift:
            embed.add_field(
                name="🎁 Welcome gift",
                value="**1 signature vinyl** is waiting for you — open it with `.sv`.",
                inline=False,
            )
        embed.add_field(
            name="Getting started",
            value="`.c` for a choice drop · `/collection` to see what you own · "
                  "`/help` for everything else",
            inline=False,
        )
        await ctx.send(embed=embed)
        return

    @commands.command(alias=['ur'])
    @commands.has_role("grails-admin")
    async def unregister(self, ctx, user: discord.User):
        """Unregister a user from the bot"""
        if not user_exists(user.id):
            await ctx.send(f"❌ **{user.name}** is not registered.")
            return

        unregister_user(user.id)
        await ctx.send(f"✅ Successfully Unregistered **{user.name}**!")
        return



# ✅ Async setup function required in discord.py v2.x
async def setup(bot):
    await bot.add_cog(RegisterCog(bot))
