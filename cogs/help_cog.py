import os, sys
import logging

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands

from utils.command_types import slash_only
from utils import aesthetics

log = logging.getLogger("grails.help")

# check-predicate qualnames that indicate an admin/permission-gated command
_ADMIN_CHECK_PREFIXES = ("has_role", "has_any_role", "has_permissions", "is_owner")

BLANK = "​"  # zero-width space: Discord requires a non-empty field name

BRIEF_MAX = 72

# The sections, and the order of commands inside them, mirror commands.md so the
# in-Discord help and the repo reference cannot tell different stories. There is
# no automatic link between the two -- reorder one, reorder the other.
#
# A command missing from here still appears, under "More". A hardcoded map would
# otherwise let a newly added command silently never show up in /help.
HELP_SECTIONS = (
    ("Drops", ("choice", "sigvinyl", "vinyl", "cooldown")),
    # No "offer": answering a trade is the Offer a card button on the request
    # itself, not a command, so there is nothing for /help to list.
    ("Trading", ("trade", "canceltrade", "gift")),
    ("Your account", ("register", "collection", "profile", "view", "xp",
                      "daily", "vinylcheck")),
    ("Catalogue", ("artists", "albums", "songs", "mythiccheck")),
    ("Games", ("songbattle", "battle", "leaderboard")),
    ("Help", ("help", "xphelp", "guide", "ping")),
)


def _is_admin_command(cmd) -> bool:
    for check in getattr(cmd, "checks", []):
        qn = getattr(check, "__qualname__", "")
        if any(qn.startswith(p) for p in _ADMIN_CHECK_PREFIXES):
            return True
    return False


def _brief(cmd) -> str:
    """One short line, trimmed on a word boundary.

    Several docstrings open with a sentence longer than a help row can hold,
    and cutting mid-word reads like a bug rather than an abbreviation.
    """
    text = (cmd.help or cmd.description or "").strip()
    if not text:
        return "—"
    line = text.split("\n")[0].strip().rstrip(".")
    if len(line) > BRIEF_MAX:
        line = line[:BRIEF_MAX].rsplit(" ", 1)[0] + "…"
    return line


def _label(cmd, slash_names) -> str:
    """Every way the command can actually be typed.

    A slash-only command has no reachable prefix form, and a hybrid has both --
    showing only the slash form would hide that `.daily` and `.cd` work. A
    slash command's aliases are never registered with Discord, so they are only
    listed when the prefix form is reachable.
    """
    prefix_ok = not any("slash_only" in getattr(c, "__qualname__", "")
                        for c in getattr(cmd, "checks", []))
    forms = []
    if cmd.name in slash_names:
        forms.append(f"`/{cmd.name}`")
    if prefix_ok:
        forms.append(f"`.{cmd.name}`")
        forms += [f"`.{a}`" for a in cmd.aliases]
    return " ".join(forms) or f"`.{cmd.name}`"


def _fill_section(embed, name, lines):
    """Add `lines` under the field `name`, split at Discord's 1024-char cap."""
    chunk, first = "", True
    for line in lines:
        if len(chunk) + len(line) + 1 > 1000:
            embed.add_field(name=name if first else BLANK, value=chunk, inline=False)
            chunk, first = "", False
        chunk += line + "\n"
    if chunk:
        embed.add_field(name=name if first else BLANK, value=chunk, inline=False)



class HelpCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="List the commands you can use")
    @slash_only()
    async def help(self, ctx):
        """List the available commands.

        Admin commands are deliberately absent. They are prefix-only and never
        appear in the slash picker, so listing them here would only invite
        permission errors from people who cannot run them. Admins use
        `.adminhelp` instead.
        """
        await ctx.defer()

        available = {c.name: c for c in self.bot.commands
                     if not c.hidden and not _is_admin_command(c)}
        slash_names = {c.name for c in self.bot.tree.get_commands()}

        embed = discord.Embed(
            title="Grails-Bot commands",
            description="`<x>` is required, `[x]` optional.",
            color=discord.Color.blurple(),
        )

        placed = set()
        for section, names in HELP_SECTIONS:
            lines = []
            for name in names:
                cmd = available.get(name)
                if cmd is None:      # renamed or removed since this map was written
                    continue
                placed.add(name)
                lines.append(f"{_label(cmd, slash_names)} — {_brief(cmd)}")
            if lines:
                _fill_section(embed, section, lines)

        # Anything the map does not know about, so nothing is ever hidden.
        leftover = [f"{_label(c, slash_names)} — {_brief(c)}"
                    for n, c in sorted(available.items()) if n not in placed]
        if leftover:
            _fill_section(embed, "More", leftover)

        await ctx.send(embed=embed)

    @commands.command(name="adminhelp", description="List the admin prefix commands")
    @commands.has_role("grails-admin")
    async def adminhelp(self, ctx):
        """List the admin-only prefix commands.

        Prefix-only and role-gated on purpose: these never reach the slash
        picker, so this is the one place they are discoverable, and only to
        people who can actually run them.
        """
        admin = sorted(
            (c for c in self.bot.commands if not c.hidden and _is_admin_command(c)),
            key=lambda c: c.name,
        )

        lines = [f"{_label(cmd, set())} — {_brief(cmd)}" for cmd in admin]

        embed = discord.Embed(
            title="🛠️ Admin commands",
            description="Prefix-only, gated behind **grails-admin** or bot owner.",
            color=discord.Color.red(),
        )
        _fill_section(embed, BLANK, lines)
        await ctx.send(embed=embed)

    @commands.hybrid_command(description="Check the bot's latency")
    @slash_only()
    async def ping(self, ctx):
        """Check the bot's latency"""
        latency = round(self.bot.latency * 1000)
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"Bot latency: **{latency}ms**",
            color=discord.Color.green() if latency < 100 else
            discord.Color.orange() if latency < 200 else discord.Color.red())
        await ctx.send(embed=embed)

    @commands.hybrid_command(description="Check the bot's guide")
    @slash_only()
    async def guide(self, ctx):
        """Check the bot's guide"""
        # The glyph sits in the description, not the title: Discord renders no
        # custom emoji in an embed title, so it would print as raw <:name:id>.
        embed = discord.Embed(
            title="Grails Wiki",
            description=f"{aesthetics.named_emoji('livvinyl')} "
                        "[The Official Grails Wiki]"
                        "(https://grails.notion.site/Grails-Official-Wiki-33c461605edc8045b4a4dec8d82fcb93)",
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(HelpCog(bot))
