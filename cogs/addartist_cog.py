import os, sys
import asyncio
import discord
from discord.ext import commands

# Add the parent directory to the path to import utils
sys.path.insert(0,
                os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.spotify_utils import get_access_token
from utils.helpers import save_albums_to_db, add_songs_to_db, add_artist_to_db
from db import remove_songs_by_artist, get_all_artists


class AddArtistCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @commands.command(aliases=['aa'])
    @commands.has_role("grails-admin")
    async def addartist(self, ctx, *, artist_name: str):
        """Add an artist's albums and songs to the database
        Usage: .addartist <Artist Name>
        Example: .addartist Taylor Swift
        """

        # Send initial message to show the command is processing
        processing_msg = await ctx.send(
            f"🎵 **Processing artist:** `{artist_name}`\n⏳ This may take a moment..."
        )

        try:
            print(f"\n--- Processing {artist_name} ---")

            # Process both albums and singles
            album_details = await add_artist_to_db(artist_name, "album")

            # Update progress message
            await processing_msg.edit(
                content=
                f"*Processing artist:** `{album_details['artist_name']}`\n✅ Albums processed\n⏳ Now processing singles..."
            )

            single_details = await add_artist_to_db(
                album_details['artist_name'], "single")

            # Calculate totals
            total_albums = album_details['albums_saved'] + single_details[
                'albums_saved']
            total_songs = album_details['songs_saved'] + single_details[
                'songs_saved']
            corrected_name = album_details['artist_name']

            # Create detailed success embed
            embed = discord.Embed(
                title="✅ Artist Added Successfully!",
                description=
                f"**{corrected_name}** has been processed and added to the database.",
                color=discord.Color.green())

            # Add detailed breakdown
            embed.add_field(
                name="📀 Albums",
                value=
                f"`{album_details['albums_saved']}` albums\n`{album_details['songs_saved']}` songs",
                inline=True)
            embed.add_field(
                name="🎵 Singles",
                value=
                f"`{single_details['albums_saved']}` albums\n`{single_details['songs_saved']}` songs",
                inline=True)
            embed.add_field(
                name="📊 Total",
                value=f"`{total_songs}` songs added to the database",
                inline=True)

            embed.set_footer(
                text="All songs and albums are now available for pulls!")

            await processing_msg.edit(content="", embed=embed)

        except Exception as e:
            print(f"Error processing {artist_name}: {str(e)}")

            error_embed = discord.Embed(
                title="❌ Error Adding Artist",
                description=f"Failed to add **{artist_name}** to the database.",
                color=discord.Color.red())
            error_embed.add_field(name="Error Details",
                                  value=f"```{str(e)[:1000]}```",
                                  inline=False)
            error_embed.set_footer(
                text="Please check the logs for more details.")

            await processing_msg.edit(content="", embed=error_embed)

    @addartist.error
    async def addartist_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send(
                "❌ You need the 'grails-admin' role to use this command!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing artist name!**\n"
                           "**Usage:** `.addartist <Artist Name>`\n"
                           "**Example:** `.addartist Taylor Swift`")
        else:
            await ctx.send(f"❌ **An error occurred:** {str(error)}")
            raise error

    @commands.command(aliases=['ra'])
    @commands.has_role("grails-admin")
    async def removeartist(self, ctx, *, artist_name: str):
        """Remove an artist and all their songs/albums from the database
        Usage: .removeartist <Artist Name>
        Example: .removeartist Taylor Swift
        """

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

        # Send confirmation message
        confirmation_msg = await ctx.send(
            f"⚠️ **Are you sure you want to remove all data for `{artist_found}`?**\n"
            f"This will permanently delete all songs and albums from this artist.\n"
            f"React with ✅ to confirm or ❌ to cancel.")

        # Add reaction options
        try:
            await confirmation_msg.add_reaction("✅")
            await confirmation_msg.add_reaction("❌")
        except discord.Forbidden:
            await ctx.send(
                "❌ **Missing Permissions!** The bot needs 'Add Reactions' permission to use this command."
            )
            return
        except Exception as e:
            await ctx.send(f"❌ **Error setting up confirmation:** {str(e)}")
            return

        def check(reaction, user):
            return user == ctx.author and str(reaction.emoji) in [
                "✅", "❌"
            ] and reaction.message.id == confirmation_msg.id

        try:
            reaction, user = await self.bot.wait_for('reaction_add',
                                                     timeout=30.0,
                                                     check=check)

            if str(reaction.emoji) == "❌":
                try:
                    await confirmation_msg.edit(
                        content=
                        f"❌ **Removal cancelled.** `{artist_found}` was not removed from the database."
                    )
                    await confirmation_msg.clear_reactions()
                except discord.Forbidden:
                    await ctx.send(
                        f"❌ **Removal cancelled.** `{artist_found}` was not removed from the database."
                    )
                except Exception as e:
                    await ctx.send(
                        f"❌ **Removal cancelled.** `{artist_found}` was not removed from the database."
                    )
                return

            elif str(reaction.emoji) == "✅":
                # Proceed with removal
                try:
                    processing_msg = await confirmation_msg.edit(
                        content=
                        f"🗑️ **Removing artist:** `{artist_found}`\n⏳ This may take a moment..."
                    )
                    await confirmation_msg.clear_reactions()
                except discord.Forbidden:
                    processing_msg = await ctx.send(
                        f"🗑️ **Removing artist:** `{artist_found}`\n⏳ This may take a moment..."
                    )
                except Exception:
                    processing_msg = await ctx.send(
                        f"🗑️ **Removing artist:** `{artist_found}`\n⏳ This may take a moment..."
                    )

                try:
                    # Remove the artist from database
                    result = remove_songs_by_artist(artist_found)

                    if result['success']:
                        # Create success embed
                        embed = discord.Embed(
                            title="✅ Artist Removed Successfully!",
                            description=
                            f"**{artist_found}** has been completely removed from the database.",
                            color=discord.Color.green())

                        # Add detailed breakdown
                        embed.add_field(
                            name="🎵 Songs Deleted",
                            value=f"`{result['songs_deleted']}` songs",
                            inline=True)
                        embed.add_field(
                            name="📀 Albums Deleted",
                            value=f"`{result['albums_deleted']}` albums",
                            inline=True)

                        embed.set_footer(
                            text=
                            "The artist and all associated data have been permanently removed."
                        )

                        try:
                            await processing_msg.edit(content="", embed=embed)
                        except discord.Forbidden:
                            await ctx.send(embed=embed)
                        except Exception:
                            await ctx.send(embed=embed)

                    else:
                        # Handle error
                        error_embed = discord.Embed(
                            title="❌ Error Removing Artist",
                            description=
                            f"Failed to remove **{artist_found}** from the database.",
                            color=discord.Color.red())
                        error_embed.add_field(
                            name="Error Details",
                            value=f"```{result['error'][:1000]}```",
                            inline=False)

                        try:
                            await processing_msg.edit(content="",
                                                      embed=error_embed)
                        except discord.Forbidden:
                            await ctx.send(embed=error_embed)
                        except Exception:
                            await ctx.send(embed=error_embed)

                except Exception as e:
                    print(f"Error removing {artist_found}: {str(e)}")

                    error_embed = discord.Embed(
                        title="❌ Error Removing Artist",
                        description=
                        f"Failed to remove **{artist_found}** from the database.",
                        color=discord.Color.red())
                    error_embed.add_field(name="Error Details",
                                          value=f"```{str(e)[:1000]}```",
                                          inline=False)

                    try:
                        await processing_msg.edit(content="",
                                                  embed=error_embed)
                    except discord.Forbidden:
                        await ctx.send(embed=error_embed)
                    except Exception:
                        await ctx.send(embed=error_embed)

        except asyncio.TimeoutError:
            try:
                await confirmation_msg.edit(
                    content=
                    f"⏰ **Timeout.** Removal of `{artist_found}` was cancelled due to no response."
                )
                await confirmation_msg.clear_reactions()
            except discord.Forbidden:
                await ctx.send(
                    f"⏰ **Timeout.** Removal of `{artist_found}` was cancelled due to no response."
                )
            except Exception:
                await ctx.send(
                    f"⏰ **Timeout.** Removal of `{artist_found}` was cancelled due to no response."
                )

    @removeartist.error
    async def removeartist_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send(
                "❌ You need the 'grails-admin' role to use this command!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing artist name!**\n"
                           "**Usage:** `.removeartist <Artist Name>`\n"
                           "**Example:** `.removeartist Taylor Swift`")
        else:
            await ctx.send(f"❌ **An error occurred:** {str(error)}")
            raise error


async def setup(bot):
    await bot.add_cog(AddArtistCog(bot))
