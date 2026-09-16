from db import list_latest, remove_latest, add_user_xp, user_exists
import discord
from discord.ext import commands
from math import ceil

from utils import aesthetics

class ModCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(aliases=['ll'])
    @commands.is_owner()
    async def list_latest(self, ctx, n: int = 10, page: int = 1):
        """List the n latest songs pulled (default 10, 25 per page)"""
        try:
            # Fetch the latest songs
            latest_songs = list_latest(limit=n)
            if not latest_songs:
                await ctx.send("No songs found in the collection.")
                return

            # Pagination logic
            songs_per_page = 25
            total_pages = ceil(len(latest_songs) / songs_per_page)
            if page < 1 or page > total_pages:
                await ctx.send(f"Invalid page number. Please choose a page between 1 and {total_pages}.")
                return

            # Get the songs for the current page
            start_index = (page - 1) * songs_per_page
            end_index = start_index + songs_per_page
            songs_on_page = latest_songs[start_index:end_index]

            # Create the embed
            embed = discord.Embed(
                title=f"🎵 Latest {n} Songs Pulled (Page {page}/{total_pages})",
                color=discord.Color.blue()
            )
            for song in songs_on_page:
                _, song_name, artist, variant, album_name, collected_at = song[:6]
                embed.add_field(
                    name=f"{song_name} - {artist}",
                    value=f"{variant} @ {collected_at}",
                    inline=False
                )

            # Send the embed
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"An error occurred: {e}")

    @commands.command(aliases=['rl'])
    @commands.is_owner()
    async def remove_latest(self, ctx, n: int = 1):
        """Remove the n latest songs pulled"""
        try:
            removed_count = remove_latest(n=n)
            await ctx.send(f"Successfully removed {removed_count} song(s) from the collection.")
        except Exception as e:
            await ctx.send(f"An error occurred: {e}")

    @commands.command(aliases=['gxp'])
    @commands.is_owner()
    async def give_xp(self, ctx, user: discord.User, xp: int):
        """Give a user x XP (x can be negative)"""
        try:
            if not user_exists(user.id):
                await ctx.send(f"❌ User {user.mention} is not registered.")
                return

            if add_user_xp(user.id, xp):
                await ctx.send(f"✅ Successfully gave {xp} XP to {user.mention}.")
            else:
                await ctx.send(f"❌ Failed to update XP for {user.mention}.")
        except Exception as e:
            await ctx.send(f"An error occurred: {e}")
    @commands.command(aliases=['ec'])
    @commands.has_role("grails-admin")
    async def emojicheck(self, ctx):
        """Show which rarity/variant card emoji the server is still missing.

        Cards are drawn with a custom emoji per rarity+variant pair, resolved by
        name at startup. Anything not uploaded falls back to a generic unicode
        glyph, which is easy to miss -- this lists the exact names to upload.
        """
        resolved = aesthetics.registered_card_emojis()
        missing = aesthetics.missing_card_emojis()
        total = len(aesthetics.RARITY_ORDER) * len(aesthetics.VARIANTS)

        embed = discord.Embed(
            title="Card emoji coverage",
            description=f"**{total - len(missing)} / {total}** pairs have a custom emoji.",
            colour=discord.Colour.green() if not missing else discord.Colour.orange(),
        )
        if missing:
            wanted = [aesthetics.card_emoji_names(r, v)[0] for r, v in missing]
            # One field per chunk: Discord caps a field value at 1024 chars.
            for i in range(0, len(wanted), 20):
                embed.add_field(
                    name="Missing" if i == 0 else "​",
                    value=" ".join(f"`{n}`" for n in wanted[i:i + 20]),
                    inline=False,
                )
            embed.set_footer(text="Upload these to the server, then run .sync or wait for the next reconnect.")
        else:
            sample = " ".join(list(resolved.values())[:12])
            embed.add_field(name="Sample", value=sample or "​", inline=False)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ModCog(bot))