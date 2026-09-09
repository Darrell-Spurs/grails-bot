import os, sys
import asyncio
import logging
import threading

from dotenv import load_dotenv

# Resolve everything from this file rather than the working directory: hosts
# frequently start the process from somewhere other than the project root.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
COGS_DIR = os.path.join(PROJECT_ROOT, "cogs")

# Must run before importing db: db.py reads DATABASE_URL at import time to pick
# its backend, so loading .env afterwards would silently ignore it.
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
sys.path.insert(0, PROJECT_ROOT)

import discord
from discord.ext import commands
from db import init_db, user_exists, register_user

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("grails.bot")

init_db()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='.', intents=intents, help_command=None)


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)


@bot.before_invoke
async def _auto_register(ctx):
    """Lazily register any user on their first command (prefix OR slash) so new
    users never hit a 'not registered' wall."""
    # Let the explicit .register command give its own welcome message.
    if ctx.command and ctx.command.name == "register":
        return
    try:
        if not user_exists(ctx.author.id):
            register_user(ctx.author.id, xp=0, username=ctx.author.name)
            log.info("Auto-registered %s (%s)", ctx.author.name, ctx.author.id)
    except Exception:
        log.exception("auto-register failed for %s", ctx.author.id)


@bot.command(name="sync")
@commands.has_role("bot_admin")
async def sync_cmd(ctx):
    """Sync slash (app) commands to this server so they appear immediately."""
    ctx.bot.tree.copy_global_to(guild=ctx.guild)
    synced = await ctx.bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"✅ Synced {len(synced)} slash commands to this server.")


@sync_cmd.error
async def sync_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.send("❌ You need the 'bot_admin' role to sync commands.")


@bot.event
async def setup_hook():
    loaded, failed = 0, 0
    for filename in sorted(os.listdir(COGS_DIR)):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        try:
            await bot.load_extension(f"cogs.{filename[:-3]}")
            loaded += 1
        except Exception:
            failed += 1
            log.exception("Failed to load cog %s", filename)
    log.info("Cogs loaded: %d (%d failed)", loaded, failed)


BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit(
        "BOT_TOKEN is not set. Add it to .env for local runs, or set it as an "
        "environment variable on your host."
    )

# The admin panel is a local tool by default: it binds loopback so it isn't
# reachable from the network. Set ENABLE_WEB_PANEL=0 on a remote host to skip it
# entirely, or WEB_HOST=0.0.0.0 if you deliberately want it exposed.
ENABLE_WEB_PANEL = os.environ.get("ENABLE_WEB_PANEL", "1").lower() not in ("0", "false", "no")
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("PORT", 8080))


def start_web_panel():
    from web.app import create_app
    app = create_app(bot)
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True, use_reloader=False)


async def main():
    if ENABLE_WEB_PANEL:
        threading.Thread(target=start_web_panel, daemon=True).start()
        log.info("Web admin panel on http://%s:%s", WEB_HOST, WEB_PORT)
    else:
        log.info("Web admin panel disabled (ENABLE_WEB_PANEL=0)")

    async with bot:
        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
