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
from db import init_db, user_exists
from utils import aesthetics
from utils.command_types import NotRegistered
from utils.errors import handle_command_error
from utils.logsetup import event, setup_logging

# Console gets the aligned, coloured format; the file gets the same layout with
# the colour stripped, so a restart no longer erases the history.
setup_logging(level=logging.INFO, logfile=os.path.join(PROJECT_ROOT, "logs", "grails.log"))
log = logging.getLogger("grails.bot")

init_db()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='.', intents=intents, help_command=None)


@bot.event
async def on_ready():
    event(log, "startup", "logged in as %s", bot.user)
    _load_card_emojis()
    # Last line of startup: cogs are loaded by setup_hook, the gateway is up and
    # the emoji registry is filled, so this is the first moment a command typed
    # in the server will actually work. on_ready fires again on every reconnect,
    # so it is announced each time rather than only once.
    event(log, "bot ready", "commands are ready to use")


def _guild_card_emojis():
    """The guild emoji whose names the palette actually asks for.

    Unrelated server emoji are dropped here so the registry stays small and a
    name collision with something else cannot shadow a card glyph.
    """
    wanted = aesthetics.wanted_emoji_names()
    return {e.name.lower(): str(e) for e in bot.emojis if e.name.lower() in wanted}


@bot.event
async def on_guild_emojis_update(guild, before, after):
    """Uploading or renaming an emoji takes effect without a restart."""
    _load_card_emojis()


def _load_card_emojis():
    """Hand the guilds' custom emoji to the aesthetics module.

    Runs on every ready (including reconnects) so an emoji uploaded while the
    bot is live starts rendering without a restart. Only names the palette
    actually asks for are kept, so unrelated server emoji cost nothing.
    """
    aesthetics.set_card_emoji_provider(_guild_card_emojis)
    found = _guild_card_emojis()
    aesthetics.set_card_emojis(found)
    aesthetics.set_named_emojis(found)
    event(log, "emoji ready", "%d of %d rarity/variant pairs resolved",
             len(aesthetics.RARITY_ORDER) * len(aesthetics.VARIANTS)
             - len(aesthetics.missing_card_emojis()),
             len(aesthetics.RARITY_ORDER) * len(aesthetics.VARIANTS))

    missing = aesthetics.missing_card_emojis()
    if missing:
        # Not an error: unresolved pairs fall back to unicode glyphs. Logged so
        # the gap is visible instead of quietly showing the wrong thing.
        log.warning("No custom emoji for %d rarity/variant pairs, e.g. %s",
                    len(missing),
                    ", ".join(f"{r}_{aesthetics.VARIANT_EMOJI_SUFFIX.get(v, v)}".rstrip("_")
                              for r, v in missing[:5]))


def _describe_param(value):
    """Render one argument compactly for the invocation log."""
    if isinstance(value, discord.abc.User):
        return f"{value}({value.id})"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


@bot.event
async def on_command(ctx):
    """One line per invocation: what was run, how, and with what.

    Fires for prefix commands and for hybrid commands used as slash commands,
    since both are dispatched through a Context. A future pure
    app_commands.command would not appear here -- it never builds a Context.
    """
    # ctx.args is [cog, ctx, *positional] inside a cog, [ctx, *positional]
    # outside one. Keyword-only parameters arrive separately in ctx.kwargs.
    start = 2 if ctx.command is not None and ctx.command.cog is not None else 1
    params = [_describe_param(v) for v in (ctx.args or [])[start:] if v is not None]
    params += [f"{k}={_describe_param(v)}" for k, v in (ctx.kwargs or {}).items() if v is not None]

    event(log, "user_command",
          "{command: %s, type: %s, param: %s}  by %s",
          ctx.command.qualified_name if ctx.command else "?",
          "slash" if ctx.interaction is not None else "prefix",
          params,
          ctx.author.name)


# Commands that must work before you have an account, or that do not touch one.
_NO_ACCOUNT_NEEDED = {"register", "help", "adminhelp", "ping"}

# Admin tooling acts on *other* people's data, so gating it on the operator's
# own player account would only get in the way.
_ADMIN_CHECK_PREFIXES = ("has_role", "has_any_role", "has_permissions", "is_owner")


def _is_admin_command(cmd):
    return any(getattr(check, "__qualname__", "").startswith(_ADMIN_CHECK_PREFIXES)
               for check in getattr(cmd, "checks", []))


@bot.before_invoke
async def _require_registration(ctx):
    """Stop unregistered users at the door instead of registering them silently.

    Accounts used to be created lazily on first command. They are not any more:
    registering is what grants the signature vinyl welcome gift, so it has to be
    a deliberate act. A database hiccup is not a reason to block play, so a
    failed lookup lets the command through and the command's own checks decide.
    """
    if ctx.command is None or ctx.command.name in _NO_ACCOUNT_NEEDED:
        return
    if _is_admin_command(ctx.command):
        return
    try:
        registered = await asyncio.to_thread(user_exists, ctx.author.id)
    except Exception:
        log.exception("registration check failed for %s", ctx.author.id)
        return
    if not registered:
        raise NotRegistered()


@bot.event
async def on_command_error(ctx, error):
    """Every command error, for every command, goes through one handler.

    Commands no longer carry their own `.error` handlers; what used to differ
    between them -- the usage text -- is in each command's `extras`. See
    utils/errors.py.
    """
    await handle_command_error(ctx, error)


@bot.command(name="sync")
@commands.has_role("grails-admin")
async def sync_cmd(ctx):
    """Sync slash (app) commands to this server so they appear immediately."""
    ctx.bot.tree.copy_global_to(guild=ctx.guild)
    synced = await ctx.bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"✅ Synced {len(synced)} slash commands to this server.")



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
