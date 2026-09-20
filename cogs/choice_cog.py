import os, sys
import asyncio
import time
import random
import logging

from db import (add_song_to_collection, get_song_and_album_details,
                add_user_xp, get_user_xp, get_mythic_copy_info, can_collect_mythic,
                get_next_mythic_copy_number, get_song_rarity,
                get_available_mythic_songs_for_user, user_owns_mythic,
                get_mythic_pull_state,
                get_mythic_hunt, set_mythic_hunt, clear_mythic_hunt, add_vinyl_count)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
from utils.helpers import (get_random_song, get_random_songs, make_3_song_collage,
                           get_random_album, draw_variant, SOURPATCH_ID, SOURPATCH_IMAGE,
                           IMAGES_DIR)
from utils.mystic import main as generate_mythic_gif
import db
from utils import economy, odds
from utils import aesthetics
from utils.errors import report_unhandled
from utils.logsetup import event

log = logging.getLogger("grails.choice")

rarity_multiplier = odds.RARITY_XP_MULTIPLIER

variant_multiplier = odds.VARIANT_XP

# ✅ View for button UI
class ChooseSongView(discord.ui.View):
    def __init__(self, ctx: commands.Context, options: list):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.options = options

    @discord.ui.button(label="1", style=discord.ButtonStyle.primary)
    async def button1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_selection(interaction, 0)

    @discord.ui.button(label="2", style=discord.ButtonStyle.primary)
    async def button2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_selection(interaction, 1)

    @discord.ui.button(label="3", style=discord.ButtonStyle.primary)
    async def button3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_selection(interaction, 2)

    async def handle_selection(self, interaction, index):
        if interaction.user != self.ctx.author:
            await interaction.response.send_message("This isn't your drop!", ephemeral=True)
            return

        song_id, song_name, artist, album, variant = self.options[index]
        album_id = album[0]

        if song_id == SOURPATCH_ID:
            await interaction.response.send_message(
                "😋 That's a **Sour Patch Kids** — sour, then sweet, but not collectible!",
                ephemeral=True)
            return

        rarity = get_song_rarity(song_id)

        # For mythic songs, check if we can still collect and get copy number
        if variant == "mythic":
            if not can_collect_mythic(song_id):
                await interaction.response.send_message("❌ This mythic has reached its maximum number of copies (3/3) and can no longer be collected!", ephemeral=True)
                return
            copy_number = get_next_mythic_copy_number(song_id)

        add_song_to_collection(interaction.user.id, song_id=song_id, album_id=album_id, variant=variant)
        event(log, "pull selected", "%s by %s [%s/%s] -> %s's collection",
               song_name, artist, rarity, variant, interaction.user.name)

        xp_cog = interaction.client.get_cog('XPCog')
        user_xp = get_user_xp(interaction.user.id)
        level_checkpoint = xp_cog.get_level_from_xp(user_xp)

        added_xp = variant_multiplier.get(variant, 1) * rarity_multiplier.get(rarity, 1)
        add_user_xp(interaction.user.id, added_xp)

        # Check for level-up after adding XP
        if xp_cog:
            await xp_cog.check_level_up(interaction.user.id, level_checkpoint, interaction.message.channel, interaction.user)

        glyph = aesthetics.card_emoji(rarity, variant)

        # Create embed for the selection message

        embed = discord.Embed(
            description=f"{glyph} **{interaction.user.display_name}** picked "
                        f"**{song_name}** by **{artist}** from *{album[1]}* "
                        f"(+{int(added_xp)} XP)",
            color=discord.Color(aesthetics.rarity_colour_int(rarity)),
        )

        await interaction.message.channel.send(embed=embed)
        await interaction.message.edit(view=None)  # Optional: remove buttons after choice

# ✅ View for mythic claim button
class MythicClaimView(discord.ui.View):
    def __init__(self, ctx: commands.Context, song_id: str, album_id: str, song_name: str, artist: str, rarity: str):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.song_id = song_id
        self.album_id = album_id
        self.song_name = song_name
        self.artist = artist
        self.rarity = rarity
        self.emoji = aesthetics.card_emoji(rarity, "mythic")
        self.claimed = False

    @discord.ui.button(label="🪄 Claim Mythic", style=discord.ButtonStyle.success)
    async def claim_mythic(self, interaction: discord.Interaction, button: discord.ui.Button):    

        if interaction.user != self.ctx.author:
            await interaction.response.send_message("This isn't your mythic pull!", ephemeral=True)
            return

        if self.claimed:
            await interaction.response.send_message("This mythic has already been claimed!", ephemeral=True)
            return

        # Check if mythic can still be collected
        if not can_collect_mythic(self.song_id):
            await interaction.response.send_message("This mythic has reached its maximum number of copies (3/3) and can no longer be collected!", ephemeral=True)
            return

        # Check if user already owns this mythic
        if user_owns_mythic(interaction.user.id, self.song_id):
            await interaction.response.send_message("You already own a copy of this mythic song!", ephemeral=True)
            return

        # Get the copy number before adding to collection
        copy_number = get_next_mythic_copy_number(self.song_id)

        # Add to collection
        add_song_to_collection(interaction.user.id, song_id=self.song_id, album_id=self.album_id, variant="mythic")

        # Add mythic XP bonus
        xp_cog = interaction.client.get_cog('XPCog')
        user_xp = get_user_xp(interaction.user.id)
        level_checkpoint = xp_cog.get_level_from_xp(user_xp)
        add_user_xp(interaction.user.id, variant_multiplier.get("mythic", 1) * rarity_multiplier.get(get_song_rarity(self.song_id), 1))

        # Check for level-up after adding XP
        if xp_cog:
            await xp_cog.check_level_up(interaction.user.id, level_checkpoint, interaction.message.channel, interaction.user)

        # Get updated copy info for display
        copy_info = get_mythic_copy_info(self.song_id)

        # Mark as claimed and disable button
        self.claimed = True
        button.disabled = True
        button.label = "✅ Claimed!"
        button.style = discord.ButtonStyle.secondary

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"{self.emoji} **{interaction.user.display_name}** claimed "
            f"**#{copy_number}** **{self.song_name}** by **{self.artist}**")

    async def on_timeout(self):
        # Disable button when view times out
        for item in self.children:
            item.disabled = True
        # Note: The message won't be updated automatically on timeout unless we have a reference to it

# ✅ Cog containing the command
def _resolve_mythic(user_id, variants, songs, ids, albums, artists):
    """Decide which mythic a pull awards. Pure database work, so it runs in a
    worker thread over one shared connection instead of half a dozen.

    Returns (song_id, song_name, artist, album_id, album_name, album_url,
    next_copy_number, rarity), or None when the user has no mythic left to
    claim. The rarity is the awarded song's own, looked up here rather than
    passed in, because the song can change inside this function.
    """
    with db.shared_connection():
        hunt = get_mythic_hunt()
        if hunt:
            song_name = hunt['song_name']
            song_id = hunt['song_id']
            album_id = hunt['album_id']
            album_name = hunt['album_name']
            album_url = hunt['album_url']
            artist = hunt['artist']
            clear_mythic_hunt()
            event(log, "mythic hunt", "triggered: %s by %s", song_name, artist)
        else:
            index = variants.index("mythic")
            song_name = songs[index]
            song_id = ids[index]
            album_id, album_name, album_url = albums[index]
            artist = artists[index]
            event(log, "mythic rolled", "%s by %s", song_name, artist)

        # Copy count, ownership and rarity in one round trip instead of four.
        state = get_mythic_pull_state(song_id, user_id)

        # Already maxed out, or this player owns it: swap in one they can claim.
        if not state['can_collect'] or state['user_owns']:
            available = get_available_mythic_songs_for_user(user_id)
            if not available:
                return None
            # Read off the state we already have -- this used to re-run the
            # copy-count query just to word a log line.
            reason = "maxed out" if not state['can_collect'] else "already owned"
            song_id, song_name, artist = random.choice(available)
            album_id, album_name, album_url = get_random_album(song_id)
            state = get_mythic_pull_state(song_id, user_id)
            event(log, "mythic swapped", "%s -> %s by %s", reason, song_name, artist)

        next_copy_number = state['next_copy_number']

        # The rarity of whichever song is actually being awarded, not of the
        # drawn pick: the hunt branch never had a pick to read, and the swap
        # above can replace the song with one of a different tier.
        rarity = state['rarity']

    return song_id, song_name, artist, album_id, album_name, album_url, next_copy_number, rarity


class ChoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # One lock per player, so two fast `.c` invocations cannot both read the
        # same charge balance and each spend it. Keyed by user id; the dict is
        # bounded by the number of people who have ever pulled this run.
        self._pull_locks = {}

    async def _maybe_sourpatch_bonus(self, ctx):
        """After any pull, a Sour Patch Kids may appear and either level the user
        up or grant a free vinyl pull. Both odds live in utils/odds.py."""
        if random.random() >= odds.SOUR_PATCH_ODDS:
            return
        xp_cog = self.bot.get_cog('XPCog')
        file = discord.File(SOURPATCH_IMAGE, filename="sourpatchkids.png")
        embed = discord.Embed(
            title="😋 A wild SOUR PATCH KIDS appeared!",
            color=discord.Color.from_rgb(140, 200, 90))
        embed.set_image(url="attachment://sourpatchkids.png")

        if random.random() < odds.SOUR_PATCH_VINYL_SPLIT or xp_cog is None:
            add_vinyl_count(ctx.author.id, 1)
            embed.description = (f"**{ctx.author.display_name}** got a "
                                 f"**free vinyl pull!** {aesthetics.named_emoji('vinyl')}")
            await ctx.send(embed=embed, file=file)
        else:
            user_xp = get_user_xp(ctx.author.id)
            level = xp_cog.get_level_from_xp(user_xp)
            needed = xp_cog.get_xp_for_level(level + 1) - user_xp
            add_user_xp(ctx.author.id, max(1, needed))
            embed.description = f"**{ctx.author.display_name}** got **leveled up to the next level!** 🆙"
            await ctx.send(embed=embed, file=file)
            await xp_cog.check_level_up(ctx.author.id, level, ctx.channel, ctx.author)

    @commands.command(name="choice", aliases=["c"], description="Get 3 random songs to choose from")
    @commands.cooldown(rate=1, per=3, type=commands.BucketType.user)
    async def choice(self, ctx):
        """Pull a drop & pick a song

        The charge is taken before any work starts, so a pull can never be had
        for free by firing the command twice quickly. The per-user lock is what
        makes that true: spend_pull_charge reads and writes in two statements,
        which two concurrent invocations could interleave.

        The retry path inside _run_choice calls _run_choice directly rather than
        this wrapper, so redrawing after an unavailable mythic does not charge
        the player a second time.
        """
        lock = self._pull_locks.setdefault(ctx.author.id, asyncio.Lock())
        async with lock:
            spent, status = await asyncio.to_thread(db.spend_pull_charge, ctx.author.id)

        if not spent:
            await ctx.send(embed=self._no_charges_embed(ctx, status))
            return

        try:
            await self._run_choice(ctx)
        except Exception:
            # The charge bought nothing, so give it back. Refunding before the
            # re-raise means the error still reaches the handler and the log.
            await asyncio.to_thread(db.add_pull_charges, ctx.author.id, economy.PULL_COST)
            event(log, "drop refunded", "%s   after a failed pull", ctx.author.name)
            raise

    def _no_charges_embed(self, ctx, status):
        """Shown when the stack is empty -- with the wait, not just a refusal."""
        wait = economy.discord_countdown(status["next_in"]) if status else "in a few minutes"
        embed = discord.Embed(
            title="Out of drops :(",
            description=f"Your next drop recharges {wait}.",
            colour=discord.Colour(aesthetics.ACCENT_COLOR_INT),
        )
        if status:
            embed.add_field(
                name=f"{status['charges']} / {status['cap']}",
                value=f"`{economy.charge_bar(status['charges'])}`",
                inline=False,
            )
        embed.set_footer(text=f"Collect up to {economy.PULL_CAP} drops at a rate of one every {economy.PULL_REGEN_SECONDS // 60} minutes. Use .c to spend them.")
        return embed

    async def _run_choice(self, ctx):
        # Prefix-only now, so there is no interaction deadline to beat; defer()
        # is a harmless no-op kept in case this ever regains a slash form.
        await ctx.defer()
        started = time.perf_counter()
        event(log, "pull requested", "%s", ctx.author.name)

        # One query for all three picks, album art and rarity included --
        # previously nine sequential round-trips.
        picks = await asyncio.to_thread(get_random_songs, 3)
        picked_ms = (time.perf_counter() - started) * 1000

        albums = [(p["album_id"], p["album_name"], p["album_image"]) for p in picks]
        songs = [p["song_name"] for p in picks]
        ids = [p["song_id"] for p in picks]
        titles = list(songs)
        artists = [p["artist"] for p in picks]
        rarities = [p["rarity"] for p in picks]
        image_urls = [p["album_image"] for p in picks]
        variants = [draw_variant(), draw_variant(), draw_variant()]
        event(log, "pull responded", "%s   [%.0fms]",
              "  |  ".join(f"{p['song_name']} [{p['rarity']}/{v}]"
                           for p, v in zip(picks, variants)),
              picked_ms)

        if "gutscookie" in variants and "mythic" not in variants:
            gutscookie_xp = odds.SOUR_PATCH_GUTSCOOKIE_XP
            cookie_path = os.path.join(IMAGES_DIR, "gutscookie.png")
            file = discord.File(cookie_path, filename="gutscookie.png")
            embed = discord.Embed(
                title=f"**{ctx.author.display_name} pulled a GUTS COOKIE**",
                description=f"Enjoy your treat and {gutscookie_xp} XP!",
                color=discord.Color.from_rgb(215, 175, 199)
            )
            embed.set_image(url="attachment://gutscookie.png")
            
            # Get XP cog and check for level-up
            xp_cog = self.bot.get_cog('XPCog')
            user_xp = get_user_xp(ctx.author.id)
            level_checkpoint = xp_cog.get_level_from_xp(user_xp) if xp_cog else 0
            
            # Add XP
            add_user_xp(ctx.author.id, gutscookie_xp)
            
            await ctx.send(embed=embed, file=file)
            
            # Check for level-up after sending the cookie message
            if xp_cog:
                await xp_cog.check_level_up(ctx.author.id, level_checkpoint, ctx.channel, ctx.author)

            await self._maybe_sourpatch_bonus(ctx)
            return

        if "mythic" in variants:
            # Resolving a mythic takes half a dozen queries. They used to run
            # inline on the event loop, one connection each; now they go to a
            # worker thread over a single shared connection.
            resolved = await asyncio.to_thread(
                _resolve_mythic, ctx.author.id, variants, songs, ids, albums, artists
            )
            if resolved is None:
                event(log, "mythic swapped", "none available for this user, redrawing")
                return await self._run_choice(ctx)
            return await self._send_mythic(ctx, resolved)

        collage_buf = await asyncio.to_thread(
            make_3_song_collage, image_urls, titles, artists, variants, rarities, None, True
        )
        file = discord.File(collage_buf, filename="choices.jpg")

        embed = discord.Embed(
            title=f"**{ctx.author.display_name}'s choices**",
            description="Click a button to pick your preferred song!",
            color=discord.Color.blurple()
        )
        embed.set_image(url="attachment://choices.jpg")

        await ctx.send(embed=embed, file=file, view=ChooseSongView(ctx, options=[(ids[i], titles[i], artists[i], albums[i], variants[i]) for i in range(3)]))
        event(log, "respond time", "%s   %.0fms total",
              ctx.author.name, (time.perf_counter() - started) * 1000)

        await self._maybe_sourpatch_bonus(ctx)

    async def _send_mythic(self, ctx, resolved):
        song_id, song_name, artist, album_id, album_name, album_url, next_copy_number, rarity = resolved

        card_emoji = aesthetics.card_emoji(rarity, "mythic")
        embed = discord.Embed(
            title=f"{card_emoji} **{ctx.author.display_name} PULLED A MYTHIC**",
            description=f"||**#{next_copy_number}**|| ||**{song_name}**|| by ||**{artist}**|| from ||*{album_name}*||  ·  "
                        f"\n\n",
            color=discord.Color.from_rgb(140, 202, 247)
        )
        claim_view = MythicClaimView(ctx, song_id, album_id, song_name, artist, rarity)

        # Post the reveal and the claim button first. Rendering the animation
        # takes the better part of a second, and that used to be dead air before
        # anything appeared at all -- the player can now read the spoilers and
        # claim immediately while the GIF is edited in behind them.
        started = time.perf_counter()
        message = await ctx.send(embed=embed, view=claim_view)

        gif_buf = await asyncio.to_thread(generate_mythic_gif, album_url, True)
        if gif_buf is None:
            log.warning("mythic gif generation returned nothing for %s", album_url)
            await message.edit(content="(Missing mythic gif \u2757)")
        else:
            # The art stays a spoilered attachment rather than an embed image:
            # the song name and artist are spoilered too, so revealing the cover
            # automatically would give the answer away before the click.
            file = discord.File(gif_buf, filename="mythic_animated.gif", spoiler=True)
            try:
                # `view` is deliberately not passed. The button may already have
                # been claimed and disabled in the time the GIF took, and an edit
                # that omits it leaves the existing components alone.
                await message.edit(attachments=[file])
            except discord.HTTPException:
                log.exception("could not attach the mythic gif for %s", song_name)
            event(log, "respond time", "mythic art for %s attached %.0fms after the reveal",
                  song_name, (time.perf_counter() - started) * 1000)

        await self._maybe_sourpatch_bonus(ctx)

    @choice.error
    async def choice_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Cool down! Try again in {round(error.retry_after)}s.")
            return
        # Anything else used to fall off the end here, which marks the error
        # handled and keeps it out of the log entirely.
        await report_unhandled(log, ctx, error, command="c")

    @commands.command()
    @commands.has_role("grails-admin")
    async def hunt(self, ctx, *, args: str):
        """Set a specific song to be the next mythic pull
        Usage: .hunt <song_name> / <album_name> / <artist>
        Example: .hunt Sports car / So Close To What / Tate McRae
        """

        # Parse the arguments separated by " / "
        try:
            parts = [part.strip() for part in args.split(' / ')]
            if len(parts) != 3:
                raise ValueError("Invalid format")

            song_name, album_name, artist = parts
        except (ValueError, IndexError):
            await ctx.send("❌ **Invalid format!**\n"
                          "**Usage:** `.hunt <song_name> / <album_name> / <artist>`\n"
                          "**Example:** `.hunt Sports car / So Close To What / Tate McRae`")
            return

        # Look up the song and album details in the database
        song_id, song_name_corrected, album_id, album_name_corrected, album_url = get_song_and_album_details(song_name, album_name, artist)

        if not song_id:
            await ctx.send(f"❌ **Song not found!**\n"
                          f"Could not find song `{song_name}` by `{artist}` in the database.")
            return

        if not album_id:
            await ctx.send(f"❌ **Album not found!**\n"
                          f"Could not find album `{album_name}` by `{artist}` in the database.")
            return

        # Store the hunt data
        set_mythic_hunt(song_id, song_name_corrected, album_id, album_name_corrected, album_url, artist)

        embed = discord.Embed(
            title="🎯 Mythic Hunt Set!",
            description=f"The next mythic pull will be:\n"
                        f"**{song_name_corrected}** by **{artist}** from *{album_name_corrected}*",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=album_url)
        embed.set_footer(text="The next user to hit mythic will get this song!")

        await ctx.send(embed=embed)

    @commands.command(aliases=["clh"])
    @commands.has_role("grails-admin")
    async def clearhunt(self, ctx):
        """Clear the current mythic hunt"""
        old_hunt = get_mythic_hunt()
        if old_hunt:
            clear_mythic_hunt()
            await ctx.send(f"🚫 Cleared mythic hunt: **{old_hunt['song_name']}** by "
                           f"**{old_hunt['artist']}** from *{old_hunt['album_name']}*")
        else:
            await ctx.send("❌ No active mythic hunt to clear.")

    @commands.command(aliases=["ckh"])
    @commands.has_role("grails-admin")
    async def checkhunt(self, ctx):
        """Check the current mythic hunt status"""
        hunt = get_mythic_hunt()
        if hunt:
            embed = discord.Embed(
                title="🎯 Current Mythic Hunt",
                description=f"**{hunt['song_name']}** by **{hunt['artist']}** from *{hunt['album_name']}*",
                color=discord.Color.gold()
            )
            embed.set_thumbnail(url=hunt['album_url'])
            embed.set_footer(text="This will be the next mythic pull!")
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ No active mythic hunt set.")

    @commands.command()
    @commands.has_role("grails-admin")
    async def mythicstatus(self, ctx, *, args: str):
        """Check the status of a mythic song's copies
        Usage: .mythicstatus <song_name> / <album_name> / <artist>
        Example: .mythicstatus Sports car / So Close To What / Tate McRae
        """

        # Parse the arguments separated by " / "
        try:
            parts = [part.strip() for part in args.split(' / ')]
            if len(parts) != 3:
                raise ValueError("Invalid format")

            song_name, album_name, artist = parts
        except (ValueError, IndexError):
            await ctx.send("❌ **Invalid format!**\n"
                          "**Usage:** `.mythicstatus <song_name> / <album_name> / <artist>`\n"
                          "**Example:** `.mythicstatus Sports car / So Close To What / Tate McRae`")
            return

        # Look up the song in the database
        song_id, song_name_corrected, album_id, album_name_corrected, album_url = get_song_and_album_details(song_name, album_name, artist)

        if not song_id:
            await ctx.send(f"❌ **Song not found!**\n"
                          f"Could not find song `{song_name}` by `{artist}` in the database.")
            return

        # Get mythic copy information
        copy_info = get_mythic_copy_info(song_id)

        embed = discord.Embed(
            title="📊 Mythic Status",
            description=f"**{song_name_corrected}** by **{artist}** from *{album_name_corrected}*",
            color=discord.Color.from_rgb(140, 202, 247)
        )

        embed.add_field(
            name="Copy Status",
            value=f"**{copy_info['current_copies']}** / **{copy_info['max_copies']}** collected\n"
                  f"**{copy_info['remaining_copies']}** remaining\n"
                  f"Can collect: {'✅ Yes' if copy_info['can_collect'] else '❌ No'}",
            inline=False
        )

        if copy_info['current_copies'] > 0:
            # Get the users who own this mythic
            from db import get_connection
            conn = get_connection()
            c = conn.cursor()
            c.execute("""
                SELECT user_id, collected_at
                FROM collections
                WHERE song_id = ? AND variant = 'mythic'
                ORDER BY collected_at ASC
            """, (song_id,))
            owners = c.fetchall()
            conn.close()

            if owners:
                owner_list = []
                for i, (user_id, collected_at) in enumerate(owners, 1):
                    owner_list.append(f"**#{i}** - <@{user_id}>")

                embed.add_field(
                    name="Owners",
                    value="\n".join(owner_list),
                    inline=False
                )

        embed.set_thumbnail(url=album_url)
        await ctx.send(embed=embed)

    @commands.command()
    @commands.has_role("grails-admin")
    async def testmythic(self, ctx, *, args: str):
        """Test the mythic copy system by checking a song's status
        Usage: .testmythic <song_name> / <album_name> / <artist>
        Example: .testmythic Sports car / So Close To What / Tate McRae
        """

        # Parse the arguments separated by " / "
        try:
            parts = [part.strip() for part in args.split(' / ')]
            if len(parts) != 3:
                raise ValueError("Invalid format")

            song_name, album_name, artist = parts
        except (ValueError, IndexError):
            await ctx.send("❌ **Invalid format!**\n"
                          "**Usage:** `.testmythic <song_name> / <album_name> / <artist>`\n"
                          "**Example:** `.testmythic Sports car / So Close To What / Tate McRae`")
            return

        # Look up the song in the database
        song_id, song_name_corrected, album_id, album_name_corrected, album_url = get_song_and_album_details(song_name, album_name, artist)

        if not song_id:
            await ctx.send(f"❌ **Song not found!**\n"
                          f"Could not find song `{song_name}` by `{artist}` in the database.")
            return

        # Get mythic copy information
        copy_info = get_mythic_copy_info(song_id)
        next_copy = get_next_mythic_copy_number(song_id)
        can_collect = can_collect_mythic(song_id)

        embed = discord.Embed(
            title="🧪 Mythic Test Results",
            description=f"**{song_name_corrected}** by **{artist}** from *{album_name_corrected}*",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Copy Information",
            value=f"Current copies: **{copy_info['current_copies']}**\n"
                  f"Max copies: **{copy_info['max_copies']}**\n"
                  f"Next copy number: **#{next_copy}**\n"
                  f"Can collect: {'✅ Yes' if can_collect else '❌ No'}\n"
                  f"Remaining: **{copy_info['remaining_copies']}**",
            inline=False
        )

        embed.set_thumbnail(url=album_url)
        await ctx.send(embed=embed)



    @hunt.error
    async def hunt_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'grails-admin' role to use this command!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing arguments!**\n"
                          "**Usage:** `.hunt <song_name> / <album_name> / <artist>`\n"
                          "**Example:** `.hunt Sports car / So Close To What / Tate McRae`")
        else:
            raise error

    @clearhunt.error
    async def clearhunt_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'grails-admin' role to use this command!")
        else:
            raise error

    @checkhunt.error
    async def checkhunt_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'grails-admin' role to use this command!")
        else:
            raise error

    @mythicstatus.error
    async def mythicstatus_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'grails-admin' role to use this command!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing arguments!**\n"
                          "**Usage:** `.mythicstatus <song_name> / <album_name> / <artist>`\n"
                          "**Example:** `.mythicstatus Sports car / So Close To What / Tate McRae`")
        else:
            raise error

    @testmythic.error
    async def testmythic_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'grails-admin' role to use this command!")
        else:
            raise error

async def setup(bot):
    await bot.add_cog(ChoiceCog(bot))
