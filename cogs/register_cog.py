import discord
from discord.ext import commands

class RegisterCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def register(self, ctx):
        await ctx.send("What's your username?")

        def check(message):
            return message.author == ctx.author and message.channel == ctx.channel

        try:
            msg = await self.bot.wait_for('message', check=check, timeout=30.0)
            username = msg.content
            await ctx.send(f"✅ Registered as **{username}**!")
        except discord.TimeoutError:
            await ctx.send("⏰ You took too long to respond!")

# ✅ Async setup function required in discord.py v2.x
async def setup(bot):
    await bot.add_cog(RegisterCog(bot))
