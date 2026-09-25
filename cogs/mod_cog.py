import asyncio
import logging
import os

from db import list_latest, remove_latest, add_user_xp, user_exists, get_all_artists
import discord
from discord.ext import commands
from math import ceil

from utils import aesthetics
from utils.vinyl import find_signature, signature_filename, signature_files

log = logging.getLogger("grails.mod")

class ModCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(aliases=['ll'])
    @commands.is_owner()
    async def list_latest(self, ctx, n: int = 10, page: int = 1):
        """List the n latest songs pulled (default 10, 25 per page)"""
        try:
            # Fetch the latest songs
            latest_songs = await asyncio.to_thread(list_latest, limit=n)
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
                # Song in the value, not the name: field names do not render
                # markdown, so bold there would print literal asterisks.
                    name=f"{variant} @ {collected_at}",
                    value=f"**{song_name}** by **{artist}** from *{album_name}*",
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
            removed_count = await asyncio.to_thread(remove_latest, n=n)
            await ctx.send(f"Successfully removed {removed_count} song(s) from the collection.")
        except Exception as e:
            await ctx.send(f"An error occurred: {e}")

    @commands.command(aliases=['gxp'])
    @commands.is_owner()
    async def give_xp(self, ctx, user: discord.User, xp: int):
        """Give a user x XP (x can be negative)"""
        try:
            if not await asyncio.to_thread(user_exists, user.id):
                await ctx.send(f"❌ User {user.mention} is not registered.")
                return

            if await asyncio.to_thread(add_user_xp, user.id, xp):
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

    @commands.command(aliases=['sc'])
    @commands.has_role("grails-admin")
    async def sigcheck(self, ctx):
        """Check which artists are missing signature art.

        A signature vinyl is only reachable if utils/images/sigs holds a file
        named for the artist, so an artist added to the catalogue without one
        will fail the moment somebody rolls a signature on them. This lists the
        gap before a player finds it.

        Resolution goes through utils.vinyl.find_signature -- the same function
        the pull uses -- so this check cannot report a file the pull would then
        fail to open.
        """
        artists = await asyncio.to_thread(get_all_artists)
        files = signature_files()

        have, missing = [], []
        matched = set()
        for artist in sorted(artists, key=str.lower):
            found = find_signature(artist)
            if found:
                have.append(artist)
                matched.add(os.path.basename(found))
            else:
                missing.append(artist)

        # Art with nobody to attach it to: usually an artist that was renamed or
        # removed from the catalogue, leaving the file behind.
        orphans = [f for f in files if f not in matched]

        embed = discord.Embed(
            title="Signature art coverage",
            description=f"**{len(have)} / {len(artists)}** artists have signature art."
                        + (f"  ·  {len(orphans)} unused file"
                           f"{'' if len(orphans) == 1 else 's'}" if orphans else ""),
            colour=discord.Colour.green() if not missing else discord.Colour.orange(),
        )

        if missing:
            self._fill_field(embed, "Missing", [f"`{a}`" for a in missing],
                             hint=f"expected e.g. `{signature_filename(missing[0])}`")
        if orphans:
            self._fill_field(embed, "Unused files", [f"`{f}`" for f in orphans])
        if have and not missing:
            embed.add_field(name="All covered",
                            value=" ".join(f"`{a}`" for a in have)[:1024], inline=False)

        embed.set_footer(text=f"{len(files)} file(s) in utils/images/sigs")
        await ctx.send(embed=embed)

    @staticmethod
    def _fill_field(embed, name, items, hint=""):
        """Add `items` to `embed`, splitting at Discord's 1024-char field cap."""
        chunk, first = "", True
        for item in items:
            if len(chunk) + len(item) + 1 > 1000:
                embed.add_field(name=name if first else "\u200b", value=chunk, inline=False)
                chunk, first = "", False
            chunk += item + " "
        if chunk:
            embed.add_field(name=name if first else "\u200b",
                            value=chunk + (f"\n{hint}" if hint else ""), inline=False)

async def setup(bot):
    await bot.add_cog(ModCog(bot))