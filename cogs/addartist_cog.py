import os, sys
import asyncio
import time
import discord
from discord.ext import commands

# Add the parent directory to the path to import utils
sys.path.insert(0,
                os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from db import remove_songs_by_artist, find_artist
from utils import artist_import

# How often the progress message is edited. Discord rate-limits edits, and a
# line that changes faster than this cannot be read anyway.
PROGRESS_EDIT_SECONDS = 2
BAR_WIDTH = 10


def progress_text(snap):
    """The live status block for a running import (see utils/artist_import.py)."""
    lines = [f"📥 **Importing** `{snap['artist'] or snap['query']}`"]
    phase = snap["phase"]
    if phase == "tracks" and snap["total"]:
        filled = round(BAR_WIDTH * snap["done"] / snap["total"])
        line = (f"{'▰' * filled}{'▱' * (BAR_WIDTH - filled)}  "
                f"{snap['done']} / {snap['total']} releases  ·  {snap['phase_label']}")
        if snap["eta"]:
            line += f"  ·  ~{snap['eta']}s left"
        lines.append(line)
    elif phase == "albums":
        lines.append(f"⏳ {snap['phase_label']}…  {snap['done']} found")
    else:
        lines.append(f"⏳ {snap['phase_label']}…")
    if sum(snap["releases"].values()):
        lines.append(f"{snap['releases']['album']} albums · {snap['releases']['single']} singles"
                     f" · {sum(snap['tracks'].values())} tracks so far")
    if snap["errors"]:
        lines.append(f"⚠️ {len(snap['errors'])} problem(s) so far — see the summary")
    return "\n".join(lines)


def result_embed(snap):
    """The finished import: what landed, or why it did not."""
    if snap["phase"] != "done":
        embed = discord.Embed(
            title="❌ Error Adding Artist",
            description=f"Failed to add **{snap['artist'] or snap['query']}** to the database.",
            color=discord.Color.red())
        embed.add_field(name="Error Details",
                        value=f"```{(snap['errors'] or ['unknown error'])[-1][:1000]}```",
                        inline=False)
        embed.set_footer(text="Please check the logs for more details.")
        return embed

    result, releases, tracks = snap["result"], snap["releases"], snap["tracks"]
    embed = discord.Embed(
        title="✅ Artist Added Successfully!",
        description=f"**{snap['artist']}** has been processed and added to the database.",
        color=discord.Color.green())
    embed.add_field(name="📀 Albums",
                    value=f"`{releases['album']}` albums\n`{tracks['album']}` songs", inline=True)
    embed.add_field(name="🎵 Singles",
                    value=f"`{releases['single']}` albums\n`{tracks['single']}` songs", inline=True)
    embed.add_field(name="📊 Total",
                    value=f"`{result['tracks']}` songs\n`{result['new_songs']}` new to the database",
                    inline=True)
    if snap["errors"]:
        shown = "\n".join(f"• {e[:180]}" for e in snap["errors"][:5])
        more = f"\n…and {len(snap['errors']) - 5} more" if len(snap["errors"]) > 5 else ""
        embed.add_field(name="⚠️ Skipped", value=shown + more, inline=False)
    embed.set_footer(text=f"Took {snap['elapsed']:.0f}s · new songs start unassigned: "
                          f"tier them with .list and .assign before they can drop")
    return embed


class AddArtistCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @commands.command(aliases=['aa'], extras={
        "missing_arg": "❌ **Missing artist name!**\n"
                       "**Usage:** `.addartist <Artist Name>`\n"
                       "**Example:** `.addartist Taylor Swift`",
    })
    @commands.has_role("grails-admin")
    async def addartist(self, ctx, *, artist_name: str):
        """Add an artist's albums and songs to the database
        Usage: .addartist <Artist Name>
        Example: .addartist Taylor Swift
        """
        # The import runs as a job on its own thread (utils/artist_import.py);
        # this only watches it, editing one message as it moves along. The
        # same job is what the web panel's import page shows.
        job, started = artist_import.start_import(
            artist_name, requested_by=str(ctx.author), source="discord")
        note = "" if started else "\n*Already being imported — following that import here.*"
        message = await ctx.send(progress_text(job.snapshot()) + note)

        shown = None
        next_edit = time.monotonic() + PROGRESS_EDIT_SECONDS
        while not job.finished.is_set():
            await asyncio.sleep(0.25)
            if time.monotonic() < next_edit:
                continue
            next_edit = time.monotonic() + PROGRESS_EDIT_SECONDS
            text = progress_text(job.snapshot()) + note
            if text != shown:
                shown = text
                try:
                    await message.edit(content=text)
                except discord.HTTPException:
                    pass    # a missed progress edit is not worth failing over

        await message.edit(content=None, embed=result_embed(job.snapshot()))

    @commands.command(aliases=['ra'], extras={
        "missing_arg": "❌ **Missing artist name!**\n"
                       "**Usage:** `.removeartist <Artist Name>`\n"
                       "**Example:** `.removeartist Taylor Swift`",
    })
    @commands.has_role("grails-admin")
    async def removeartist(self, ctx, *, artist_name: str):
        """Remove an artist and all their songs/albums from the database
        Usage: .removeartist <Artist Name>
        Example: .removeartist Taylor Swift
        """

        # Check if artist exists in database
        artist_found = await asyncio.to_thread(find_artist, artist_name)

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
                    result = await asyncio.to_thread(remove_songs_by_artist, artist_found)

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

async def setup(bot):
    await bot.add_cog(AddArtistCog(bot))
