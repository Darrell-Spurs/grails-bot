import os, sys
import logging

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands

from utils.command_types import slash_only
from utils.errors import report_unhandled

log = logging.getLogger("grails.help")

# check-predicate qualnames that indicate an admin/permission-gated command
_ADMIN_CHECK_PREFIXES = ("has_role", "has_any_role", "has_permissions", "is_owner")

BLANK = "​"  # zero-width space: Discord requires a non-empty field name


def _is_admin_command(cmd) -> bool:
    for check in getattr(cmd, "checks", []):
        qn = getattr(check, "__qualname__", "")
        if any(qn.startswith(p) for p in _ADMIN_CHECK_PREFIXES):
            return True
    return False


def _brief(cmd) -> str:
    text = (cmd.help or cmd.description or "").strip()
    return text.split("\n")[0] if text else "—"


def _fill(embed, lines):
    """Add `lines` to `embed`, split across fields at Discord's 1024-char cap."""
    if not lines:
        embed.add_field(name=BLANK, value="No commands found.", inline=False)
        return
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 2 > 1000:
            embed.add_field(name=BLANK, value=chunk, inline=False)
            chunk = ""
        chunk += line + "\n\n"
    if chunk:
        embed.add_field(name=BLANK, value=chunk, inline=False)


class HelpCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="List the commands you can use")
    @slash_only()
    async def help(self, ctx):
        """List the commands available to players.

        Admin commands are deliberately absent. They are prefix-only and never
        appear in the slash picker, so listing them here would only invite
        permission errors from people who cannot run them. Admins use
        `.adminhelp` instead.
        """
        await ctx.defer()

        visible = sorted(
            (c for c in self.bot.commands if not c.hidden and not _is_admin_command(c)),
            key=lambda c: c.name,
        )
        slash_names = {c.name for c in self.bot.tree.get_commands()}

        lines = []
        for cmd in visible:
            # Show each command the way it is actually invoked. A slash command
            # has no prefix aliases worth advertising.
            is_slash = cmd.name in slash_names
            label = ("/" if is_slash else ".") + cmd.name
            aliases = ""
            if cmd.aliases and not is_slash:
                aliases = "  *(aka " + ", ".join("." + a for a in cmd.aliases) + ")*"
            lines.append("**`" + label + "`**" + aliases + "\n" + _brief(cmd))

        embed = discord.Embed(
            title="🎵 Grails-Bot commands",
            description="Everything you can use right now.",
            color=discord.Color.blurple(),
        )
        _fill(embed, lines)
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

        lines = []
        for cmd in admin:
            aliases = ""
            if cmd.aliases:
                aliases = "  *(aka " + ", ".join("." + a for a in cmd.aliases) + ")*"
            lines.append("**`." + cmd.name + "`**" + aliases + "\n" + _brief(cmd))

        embed = discord.Embed(
            title="🛠️ Admin commands",
            description="Prefix-only, gated behind **grails-admin** or bot owner.",
            color=discord.Color.red(),
        )
        _fill(embed, lines)
        await ctx.send(embed=embed)

    @adminhelp.error
    async def adminhelp_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the **grails-admin** role to use that.")
            return
        await report_unhandled(log, ctx, error, command="adminhelp")

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
        embed = discord.Embed(
            title="Grails Wiki",
            description="[Click here to open the official Grails Wiki](https://grails.notion.site/Grails-Official-Wiki-33c461605edc8045b4a4dec8d82fcb93)",
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(HelpCog(bot))
