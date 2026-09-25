import asyncio
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
from db import get_song_and_album_details, set_song_rarity, assign_basic_bulk
from utils import aesthetics

rarity_abbr = {
    "b": "basic",
    "u": "unique",
    "e": "elite",
    "l": "legendary",
    "ul": "ultimate"
}

def _lookup_and_assign(song_name, album_name, artist_name, rarity):
    """get_song_and_album_details, then set the rarity if both were found."""
    details = get_song_and_album_details(song_name, album_name, artist_name)
    if details[0] and details[2]:
        set_song_rarity(details[0], rarity)
    return details


class AssignCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Store active lists per channel
        self.active_lists = {}

    def set_active_list(self, channel_id, song_list):
        """Set the active list for a channel"""
        self.active_lists[channel_id] = song_list

    def get_active_list(self, channel_id):
        """Get the active list for a channel"""
        return self.active_lists.get(channel_id, None)

    @commands.command(aliases=['a'])
    @commands.has_role("grails-admin")
    async def assign(self, ctx, number_or_song: str = None, rarity: str = None):
        """
        Assign rarity to a song.
        Usage: .assign <number> <rarity> (assigns from active list)
        Usage: .assign 0 (assigns basic to all songs except ultimate/legendary/elite/unique)

        Valid rarities: basic, unique, elite, legendary, ultimate
        """
        
        if not number_or_song:
            await ctx.send("❌ **Missing arguments!**\n"
                          "**Usage:** `.assign <number> <rarity>` (from active list)\n"
                          "**Usage:** `.assign 0` (assigns basic to all songs)\n"
                          "**Valid rarities:** `basic`, `unique`, `elite`, `legendary`, `ultimate`")
            return

        # Handle .assign 0 case (assign all basic)
        if number_or_song == "0":
            await self._assign_all_basic(ctx)
            return

        if not rarity:
            await ctx.send("❌ **Missing rarity!**\n"
                          "**Usage:** `.assign <number> <rarity>` (from active list)\n"
                          "**Usage:** `.assign 0` (assigns basic to all songs)\n"
                          "**Valid rarities:** `basic`, `unique`, `elite`, `legendary`, `ultimate`")
            return

        # Validate rarity
        if not (rarity.lower() in rarity_abbr.keys() or rarity.lower() in rarity_abbr.values()):
            await ctx.send(f"❌ **Invalid rarity!**\n"
                          f"Valid rarities: {', '.join([f'`{r}`' for r in rarity_abbr.values()])}")
            return

        if len(rarity) <= 2:
            rarity = rarity_abbr.get(rarity.lower(), rarity)
        print(f"Assigning rarity: {rarity}")

        # Check if it's a number (for active list assignment)
        try:
            song_number = int(number_or_song)
            if song_number == 0:
                await self._assign_all_basic(ctx)
            else:
                await self._assign_from_list(ctx, song_number, rarity.lower())
        except ValueError:
            pass

    async def _assign_from_list(self, ctx, song_number, rarity):
        """Assign rarity using song number from active list"""
        active_list = self.get_active_list(ctx.channel.id)
        
        if not active_list:
            await ctx.send("❌ **No active list!**\n"
                          "Use the `.list` command first to create an active list, then use `.assign <number> <rarity>`.")
            return

        if song_number < 1 or song_number > len(active_list):
            await ctx.send(f"❌ **Invalid number!**\n"
                          f"Please choose a number between 1 and {len(active_list)}.")
            return

        # Get the song from the active list (convert to 0-based index)
        song_info = active_list[song_number - 1]
        song_id = song_info['id'] 
        song_name = song_info['name']
        artist_name = song_info['artist']
        album_name = song_info['album']

        await self._perform_assignment(ctx, song_id, song_name, artist_name, album_name, rarity, song_number)

    async def _perform_assignment(self, ctx, song_id, song_name, artist_name, album_name, rarity, song_number=None):
        """Perform the actual rarity assignment"""
        # Look the song up and set its rarity in one thread hop
        song_id, song_name_db, album_id, album_name_db, album_url = await asyncio.to_thread(
            _lookup_and_assign, song_name, album_name, artist_name, rarity)

        if not song_id:
            await ctx.send(f"❌ **Song not found!**\n"
                          f"Could not find `{song_name}` by `{artist_name}` in the database.")
            return

        if not album_id:
            await ctx.send(f"❌ **Album not found!**\n"
                          f"Could not find album `{album_name}` by `{artist_name}` in the database.")
            return

        embed = discord.Embed(
            title="✅ Rarity Assigned Successfully!",
            color=self._get_rarity_color(rarity)
        )

        embed.add_field(
            name="Details",
            value=f"**{song_name_db}** by **{artist_name}** from *{album_name_db}*\n"
                  f"Track {song_number}  ·  assigned to rarity **{rarity}**",
            inline=False
        )

        if album_url:
            embed.set_thumbnail(url=album_url)

        await ctx.send(embed=embed)

    async def _assign_all_basic(self, ctx):
        """Assign basic rarity to all songs in active list that aren't ultimate, legendary, elite, or unique"""
        active_list = self.get_active_list(ctx.channel.id)
        
        if not active_list:
            await ctx.send("❌ **No active list!**\n"
                          "Use the `.list` command first to create an active list, then use `.assign 0` to assign all basic.")
            return
        
        # One UPDATE for the whole list, by the ids the list already holds.
        # This was three round trips per song: a name lookup, a rarity read and
        # the write -- and the lookup by name could land on a namesake.
        assigned_count, missing = await asyncio.to_thread(
            assign_basic_bulk, [song_info['id'] for song_info in active_list])
        missing = set(missing)
        failed_assignments = [f"{i+1}. {song_info['name']}"
                              for i, song_info in enumerate(active_list) if song_info['id'] in missing]

        # Create success embed
        embed = discord.Embed(
            title="✅ Bulk Assignment Complete!",
            color=self._get_rarity_color("basic")
        )
        
        if assigned_count > 0:
            embed.add_field(
                name="Successfully Assigned",
                value=f"Assigned **basic** rarity to {assigned_count} songs\n",
                inline=False
            )
        
        if failed_assignments:
            failed_text = "\n".join(failed_assignments[:10])  # Limit to first 10 failures
            if len(failed_assignments) > 10:
                failed_text += f"\n...and {len(failed_assignments) - 10} more"
            
            embed.add_field(
                name="Failed Assignments",
                value=failed_text,
                inline=False
            )
        
        embed.set_footer(text="Note: Songs with ultimate, legendary, elite, or unique rarity were preserved")
        await ctx.send(embed=embed)

    def _get_rarity_color(self, rarity):
        return discord.Color(aesthetics.rarity_colour_int(rarity))

async def setup(bot):
    await bot.add_cog(AssignCog(bot))
