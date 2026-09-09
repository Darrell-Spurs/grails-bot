import sys, os

sys.path.insert(0,
                os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
from db import get_songs_by_artist_and_album_category, get_album_category, get_all_artists, get_song_by_artist, get_song_rarity


class ListCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    def _create_active_list(self, results, list_type="songs"):
        """Create an active list from results for use with assign command"""
        active_list = []
        for i, result in enumerate(results):
            song_id, song_name, album_name, track_count = result
            # Extract artist from the first result (assuming all results are from same artist)
            active_list.append({
                'id': song_id,
                'number': i + 1,
                'name': song_name,
                'album': album_name,
                'artist': None,  # Will be set by the calling function
                'track_count': track_count
            })
        return active_list

    def _set_active_list(self, ctx, active_list):
        """Set the active list in assign_cog if it exists"""
        assign_cog = self.bot.get_cog('AssignCog')
        if assign_cog:
            assign_cog.set_active_list(ctx.channel.id, active_list)

    @commands.command()
    @commands.has_role("bot_admin")
    async def list(self, ctx, *, args: str = None):
        """
        List songs by artist and album category.
        Usage: .list <artist_name> / <album_name>
        Special case: .list <artist_name> / singles (lists all singles by artist)
        List all: .list <artist_name> (lists all albums/EPs)

        Album categories based on track count:
        - 8+ songs: Album
        - 4-7 songs: EP
        - 3 or less: Single
        """

        if not args:
            await ctx.send(
                "❌ **Missing artist name!**\n"
                "**Usage:** `.list <artist_name> / <album_name>`\n"
                "**Special:** `.list <artist_name> / singles (for singles only)`\n"
                "**List all:** `.list <artist_name>` (lists all albums/EPs)")
            return

        # Parse artist name and album name from the input
        if " / " in args:
            artist_name, album_name = args.split(" / ", 1)
            artist_name = artist_name.strip()
            album_name = album_name.strip()
        else:
            artist_name = args.strip()
            album_name = None

        # Check if artist exists in database
        all_artists = get_all_artists()
        artist_found = None
        for artist in all_artists:
            if artist.lower() == artist_name.lower():
                artist_found = artist
                break

        if not artist_found:
            await ctx.send(
                f"❌ **Artist not found!**\n"
                f"Could not find artist `{artist_name}` in the database.")
            return

        # Get songs based on parameters
        results = get_songs_by_artist_and_album_category(
            artist_found, album_name)

        # Get total song count for the artist (for display purposes)
        if album_name:
            # For specific album or singles, use the current results count
            total_songs = len(results)
        else:
            # For albums/EPs listing, get all songs by the artist
            all_artist_songs = get_songs_by_artist_and_album_category(
                artist_found, None)
            total_songs = len(all_artist_songs)

        if not results:
            if album_name == "singles":
                await ctx.send(
                    f"❌ **No singles found!**\n"
                    f"No singles (3 or fewer tracks) found for `{artist_found}`."
                )
            elif album_name:
                await ctx.send(
                    f"❌ **Album not found!**\n"
                    f"Could not find album `{album_name}` by `{artist_found}` in the database."
                )
            else:
                await ctx.send(
                    f"❌ **No songs found!**\n"
                    f"No songs found for `{artist_found}` in the database.")
            return

        # Format the response
        if album_name == "singles":
            # Show only songs from singles albums (no grouping by album)
            embed = discord.Embed(
                title=f"🎵 Singles by {artist_found}",
                description="Songs from albums with 3 or fewer tracks:",
                color=discord.Color.green())

            song_list = []
            for i, (song_id, song_name, album_name_result,
                    track_count) in enumerate(results):
                rarity = get_song_rarity(song_id)
                if rarity in ["basic", "unique", "elite", "legendary", "ultimate"]:
                    song_list.append(f"{i+1}. {song_name} | {rarity}")
                else:
                    song_list.append(f"{i+1}. {song_name} | *N/A*")

            # Split into chunks if too many songs
            song_text = "\n".join(song_list)
            if len(song_text) > 1024:
                # Split into multiple fields if too long
                chunks = [
                    song_list[i:i + 20] for i in range(0, len(song_list), 20)
                ]
                for i, chunk in enumerate(chunks):
                    field_name = "Songs" if i == 0 else f"Songs (continued)"
                    embed.add_field(name=field_name,
                                    value="\n".join(chunk),
                                    inline=False)
            else:
                embed.add_field(name="Songs", value=song_text, inline=False)

        elif album_name:
            # Show songs from specific album
            song_list = []
            track_count = results[0][3] if results else 0
            category = get_album_category(track_count)

            embed = discord.Embed(
                title=f"💿 {album_name} by {artist_found}",
                description=f"{category} • {track_count} tracks",
                color=discord.Color.blue())

            for i, (song_id, song_name, _, _) in enumerate(results):
                rarity = get_song_rarity(song_id)
                if rarity in ["basic", "unique", "elite", "legendary", "ultimate"]:
                    song_list.append(f"{i+1}. {song_name} | {rarity}")
                else:
                    song_list.append(f"{i+1}. {song_name} | *N/A*")

            # Split into chunks if too many songs
            song_text = "\n".join(song_list)
            print("LENGTH", len(song_text))
            print(song_text)
            if len(song_text) > 1024:
                # Split into multiple fields if too long
                chunks = [
                    song_list[i:i + 20] for i in range(0, len(song_list), 20)
                ]
                for i, chunk in enumerate(chunks):
                    field_name = "Songs" if i == 0 else f"Songs (continued {i+1})"
                    embed.add_field(name=field_name,
                                    value="\n".join(chunk),
                                    inline=False)
            else:
                embed.add_field(name="Songs", value=song_text, inline=False)
        else:
            total_songs = len(get_song_by_artist(artist_found))
            print(f"Total songs by {artist_found}: {total_songs}")

            # Show only albums and EPs (no singles, no individual songs)
            albums_dict = {}
            for song_name, album_name_result, track_count in results:
                if track_count >= 4:  # Only include albums (8+) and EPs (4-7), exclude singles (<=3)
                    if album_name_result not in albums_dict:
                        albums_dict[album_name_result] = {
                            'track_count': track_count
                        }

            if not albums_dict:
                await ctx.send(
                    f"❌ **No albums or EPs found!**\n"
                    f"No albums or EPs (4+ tracks) found for `{artist_found}`."
                )
                return

            embed = discord.Embed(
                title=f"💽 Albums & EPs by {artist_found}",
                description=
                f"{artist_found} has {total_songs} songs in total\nFound {len(albums_dict)} albums/EPs (singles excluded):",
                color=discord.Color.purple())

            # Limit to first 20 albums to avoid embed limit
            album_count = 0
            album_list = []
            album_list_item = list(albums_dict.items())
            album_list_item.reverse()  # Reverse to show latest albums first
            for album, data in album_list_item:
                if album_count >= 20:
                    album_list.append(
                        f"- ...and {len(album_list_item) - 20} more")
                    break

                track_count = data['track_count']
                category = get_album_category(track_count)
                album_list.append(
                    f"- **{album}** • {category}, {track_count} tracks")
                album_count += 1

            # Split into chunks if too many albums
            album_text = "\n".join(album_list)
            if len(album_text) > 1024:
                # Split into multiple fields if too long
                chunks = [
                    album_list[i:i + 15]
                    for i in range(0, len(album_list), 15)
                ]
                for i, chunk in enumerate(chunks):
                    field_name = "Albums & EPs" if i == 0 else f"Albums & EPs (continued {i+1})"
                    embed.add_field(name=field_name,
                                    value="\n".join(chunk),
                                    inline=False)
            else:
                embed.add_field(name="Albums & EPs",
                                value=album_text,
                                inline=False)

        embed.set_footer(
            text=
            f"Album Categories: 8+ tracks = Album | 4-7 tracks = EP | ≤3 tracks = Single"
        )

        # Create and set active list for assign command (only for singles and specific album)
        if album_name == "singles" or (album_name and album_name != "singles"):
            active_list = self._create_active_list(results)
            # Set artist for all items in the list
            for item in active_list:
                item['artist'] = artist_found
            self._set_active_list(ctx, active_list)

            # Add numbering to embed for easier assignment
            if len(active_list
                   ) <= 20:  # Only show numbers if list isn't too long
                embed.description += f"\n\n*Use `.assign <number> <rarity>` to assign rarities (1-{len(active_list)})*"

        await ctx.send(embed=embed)

    @list.error
    async def list_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send(
                "❌ You need the 'bot_admin' role to use this command!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                "❌ **Missing arguments!**\n"
                "**Usage:** `.list <artist_name> / <album_name>`\n"
                "**Special:** `.list <artist_name> / singles` (for singles only)\n"
                "**List all:** `.list <artist_name>` (lists all albums/EPs)")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(ListCog(bot))
