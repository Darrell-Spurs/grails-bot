import discord
from discord.ext import commands
import os, sys
import asyncio
import logging
from db import init_db

sys.path.insert(0, os.path.abspath("."))

init_db()
logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='.', intents=intents)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")


@bot.event
async def setup_hook():
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            try:
                await bot.load_extension(f"cogs.{filename[:-3]}")
                print(f"✅ Loaded: {filename}")
            except Exception as e:
                print(f"❌ Failed to load {filename}: {e}")


BOT_TOKEN = os.environ['BOT_TOKEN']


async def main():
    async with bot:
        await bot.start(BOT_TOKEN)


asyncio.run(main())
