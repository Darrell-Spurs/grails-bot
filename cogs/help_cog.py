import os, sys
import logging

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands

log = logging.getLogger("grails.help")

# check-predicate qualnames that indicate an admin/permission-gated command
_ADMIN_CHECK_PREFIXES = ("has_role", "has_any_role", "has_permissions", "is_owner")


def _is_admin_command(cmd) -> bool:
    for check in getattr(cmd, "checks", []):
        qn = getattr(check, "__qualname__", "")
        if any(qn.startswith(p) for p in _ADMIN_CHECK_PREFIXES):
            return True
    return False


def _brief(cmd) -> str:
    text = (cmd.help or cmd.description or "").strip()
    return text.split("\n")[0] if text else "—"


class HelpCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(description="List commands. Use '.help admin' for admin commands.")
    async def help(self, ctx, *, section: str = None):
        """Show available commands. `.help` for user commands, `.help admin` for admin commands."""
        show_admin = bool(section) and section.strip().lower() == "admin"

        # Collect real, registered commands (dedupes aliases automatically).
        commands_list = [
            cmd for cmd in self.bot.commands
            if not cmd.hidden and _is_admin_command(cmd) == show_admin
        ]
        commands_list.sort(key=lambda c: c.name)

        if show_admin:
            embed = discord.Embed(
                title="🛠️ Admin Commands",
                description="Commands that require the **bot_admin** role.",
                color=discord.Color.red())
        else:
            embed = discord.Embed(
                title="🎵 Grails-Bot Commands",
                description="Everything you can use. Core commands also work as **/slash** commands.",
                color=discord.Color.blurple())

        if not commands_list:
            embed.add_field(name="​", value="No commands found.", inline=False)
        else:
            lines = []
            for cmd in commands_list:
                aliases = f"  *(aka {', '.join('.' + a for a in cmd.aliases)})*" if cmd.aliases else ""
                lines.append(f"**`.{cmd.name}`**{aliases}\n{_brief(cmd)}")
            # Split into <=1024-char fields (Discord field-value limit).
            chunk = ""
            for line in lines:
                if len(chunk) + len(line) + 2 > 1000:
                    embed.add_field(name="​", value=chunk, inline=False)
                    chunk = ""
                chunk += line + "\n\n"
            if chunk:
                embed.add_field(name="​", value=chunk, inline=False)

        if not show_admin:
            embed.set_footer(text="Use .help admin to see admin commands")
        await ctx.send(embed=embed)

    @commands.hybrid_command(description="Check the bot's latency")
    async def ping(self, ctx):
        """Check the bot's latency"""
        latency = round(self.bot.latency * 1000)
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"Bot latency: **{latency}ms**",
            color=discord.Color.green() if latency < 100 else
            discord.Color.orange() if latency < 200 else discord.Color.red())
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(HelpCog(bot))
