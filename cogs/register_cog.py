import discord
from discord.ext import commands
from db import register_user, user_exists, unregister_user

class RegisterCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(description="Register with the bot to start collecting")
    async def register(self, ctx):
        # Check if user is already registered
        if user_exists(ctx.author.id):
            await ctx.send(f"❌ **{ctx.author.display_name}**, you are already registered! You can start collecting XPs right away.")
            return

        register_user(ctx.author.id, xp=0, username=ctx.author.name)  # Register user in the database
        await ctx.send(f"✅ Successfully Registered as **{ctx.author.display_name}**, you can now start to collect **XP**s from playing the game!")
        return

    @commands.command(alias=['ur'])
    @commands.has_role("bot_admin")
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
