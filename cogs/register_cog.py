import asyncio
import logging

import discord
from discord.ext import commands

from utils import aesthetics
from db import register_user, unregister_user
from utils.command_types import slash_only

log = logging.getLogger("grails.register")

# Signature vinyls every new account starts with.
WELCOME_SIG_VINYLS = 1

class RegisterCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(description="Register an account to start collecting")
    @slash_only()
    async def register(self, ctx):
        # The account and its welcome gift are one INSERT, which also answers
        # "already registered": checked and created separately, two /register
        # calls sent together could both be told to go ahead and both be gifted.
        created = await asyncio.to_thread(register_user, ctx.author.id, 0, ctx.author.name,
                                          WELCOME_SIG_VINYLS)
        if not created:
            await ctx.send(f"❌ **{ctx.author.display_name}**, you are already registered! You can start collecting XPs right away.")
            return
        gift = WELCOME_SIG_VINYLS

        embed = discord.Embed(
            title=f"Welcome to Grails {aesthetics.named_emoji('vinyl')}",
            description=f"Registered as **{ctx.author.display_name}** — you can start "
                        f"collecting **XP** right away.",
            colour=aesthetics.ACCENT_COLOR_INT,
        )
        if gift:
            embed.add_field(
                name="Welcome gift 🎁",
                value=f"**1 signature vinyl** {aesthetics.named_emoji('sig_vinyl')} is waiting for you — open it with `.sv`.",
                inline=False,
            )
        embed.add_field(
            name="Getting started",
            value="`.c` for a drop · `/collection` to see what you own · `/help` for more info · `/guide` for the official wiki",
            inline=False,
        )
        await ctx.send(embed=embed)
        return

    # `aliases`, not `alias`: discord.py ignores an unknown keyword, so `.ur`
    # never worked under the old spelling.
    @commands.command(aliases=['ur'])
    @commands.has_role("grails-admin")
    async def unregister(self, ctx, user: discord.User):
        """Unregister a user from the bot"""
        # The DELETE reports whether there was an account to remove.
        if not await asyncio.to_thread(unregister_user, user.id):
            await ctx.send(f"❌ **{user.name}** is not registered.")
            return

        await ctx.send(f"✅ Successfully Unregistered **{user.name}**!")
        return



# ✅ Async setup function required in discord.py v2.x
async def setup(bot):
    await bot.add_cog(RegisterCog(bot))
