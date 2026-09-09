import os, sys
import asyncio
import random
import logging

from db import (add_song_to_collection, get_song_and_album_details,
                add_user_xp, get_user_xp, get_mythic_copy_info, can_collect_mythic,
                get_next_mythic_copy_number, get_song_rarity,
                get_available_mythic_songs_for_user, user_owns_mythic,
                get_mythic_hunt, set_mythic_hunt, clear_mythic_hunt, add_vinyl_count)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
from utils.helpers import (get_random_song, make_3_song_collage, get_random_album,
                           draw_variant, SOURPATCH_ID, SOURPATCH_IMAGE, IMAGES_DIR)
from utils.mystic import main as generate_mythic_gif

log = logging.getLogger("grails.choice")

rarity_multiplier = {
    "ultimate": 3,
    "legendary": 2.5,
    "elite": 2,
    "unique": 1.5,
    "basic": 1
}

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
        log.info("Added %s %s by %s to %s's collection", variant, song_name, artist, interaction.user.display_name)

        xp_cog = interaction.client.get_cog('XPCog')
        user_xp = get_user_xp(interaction.user.id)
        level_checkpoint = xp_cog.get_level_from_xp(user_xp)

        if variant == "mythic":
            # Add mythic XP bonus
            add_user_xp(interaction.user.id, 1200 * rarity_multiplier.get(rarity, 1))
        elif variant == "sketch":
            # Add rare XP bonus
            add_user_xp(interaction.user.id, 500 * rarity_multiplier.get(rarity, 1))
        elif variant == "glitched":
            # Add glitched XP bonus
            add_user_xp(interaction.user.id, 100 * rarity_multiplier.get(rarity, 1))
        else:
            # Add normal XP bonus
            add_user_xp(interaction.user.id, 30 * rarity_multiplier.get(rarity, 1))

        # Check for level-up after adding XP
        if xp_cog:
            await xp_cog.check_level_up(interaction.user.id, level_checkpoint, interaction.message.channel, interaction.user)

        variant_text = f" *({variant})*" if variant != "default" else ""
        copy_text = f" **#{copy_number}**" if variant == "mythic" else ""


        # Create embed for the selection message

        color_map = {
            "ultimate": discord.Color.gold(),
            "legendary": discord.Color.orange(),
            "elite": discord.Color.red(),
            "unique": discord.Color.purple(),
            "basic": discord.Color.blue(),
        }

        # Add variant-specific color and emoji
        embed = discord.Embed(
            description=f"**{interaction.user.display_name}** picked **{song_name}**{variant_text}{copy_text} by **{artist}**",
            color=color_map.get(rarity, discord.Color.blue())
        )

        await interaction.message.channel.send(embed=embed)
        await interaction.message.edit(view=None)  # Optional: remove buttons after choice

# ✅ View for mythic claim button
class MythicClaimView(discord.ui.View):
    def __init__(self, ctx: commands.Context, song_id: str, album_id: str, song_name: str, artist: str):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.song_id = song_id
        self.album_id = album_id
        self.song_name = song_name
        self.artist = artist
        self.claimed = False

    @discord.ui.button(label="🪄 Claim Mythic", style=discord.ButtonStyle.success, emoji="💎")
    async def claim_mythic(self, interaction: discord.Interaction, button: discord.ui.Button):    

        if interaction.user != self.ctx.author:
            await interaction.response.send_message("This isn't your mythic pull!", ephemeral=True)
            return

        if self.claimed:
            await interaction.response.send_message("This mythic has already been claimed!", ephemeral=True)
            return

        # Check if mythic can still be collected
        if not can_collect_mythic(self.song_id):
            await interaction.response.send_message("❌ This mythic has reached its maximum number of copies (3/3) and can no longer be collected!", ephemeral=True)
            return

        # Check if user already owns this mythic
        if user_owns_mythic(interaction.user.id, self.song_id):
            await interaction.response.send_message("❌ You already own a copy of this mythic song!", ephemeral=True)
            return

        # Get the copy number before adding to collection
        copy_number = get_next_mythic_copy_number(self.song_id)

        # Add to collection
        add_song_to_collection(interaction.user.id, song_id=self.song_id, album_id=self.album_id, variant="mythic")

        # Add mythic XP bonus
        xp_cog = interaction.client.get_cog('XPCog')
        user_xp = get_user_xp(interaction.user.id)
        level_checkpoint = xp_cog.get_level_from_xp(user_xp)
        add_user_xp(interaction.user.id, 1200 * rarity_multiplier.get(get_song_rarity(self.song_id), 1))
        
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
        await interaction.followup.send(f"**{interaction.user.display_name}** claimed the mythic: **{self.song_name} #{copy_number}** by **{self.artist}**! 💎")

    async def on_timeout(self):
        # Disable button when view times out
        for item in self.children:
            item.disabled = True
        # Note: The message won't be updated automatically on timeout unless we have a reference to it

# ✅ Cog containing the command
class ChoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _maybe_sourpatch_bonus(self, ctx):
        """1% chance after a pull: a Sour Patch Kids appears and either levels the
        user up to the next level or grants a free vinyl pull (50/50)."""
        if random.random() >= 0.01:
            return
        xp_cog = self.bot.get_cog('XPCog')
        file = discord.File(SOURPATCH_IMAGE, filename="sourpatchkids.png")
        embed = discord.Embed(
            title="😋 A wild SOUR PATCH KIDS appeared!",
            color=discord.Color.from_rgb(140, 200, 90))
        embed.set_image(url="attachment://sourpatchkids.png")

        if random.random() < 0.5 or xp_cog is None:
            add_vinyl_count(ctx.author.id, 1)
            embed.description = f"**{ctx.author.display_name}** got a **free vinyl pull!** 💿"
            await ctx.send(embed=embed, file=file)
        else:
            user_xp = get_user_xp(ctx.author.id)
            level = xp_cog.get_level_from_xp(user_xp)
            needed = xp_cog.get_xp_for_level(level + 1) - user_xp
            add_user_xp(ctx.author.id, max(1, needed))
            embed.description = f"**{ctx.author.display_name}** got **leveled up to the next level!** 🆙"
            await ctx.send(embed=embed, file=file)
            await xp_cog.check_level_up(ctx.author.id, level, ctx.channel, ctx.author)

    @commands.hybrid_command(name="choice", aliases=["c"], description="Get 3 random songs to choose from")
    @commands.cooldown(rate=1, per=5, type=commands.BucketType.user)
    async def choice(self, ctx):

        # Fetch 3 random songs (DB work offloaded so the event loop stays free)
        def _fetch_three():
            picks = [get_random_song() for _ in range(3)]
            albums_ = [get_random_album(p[1]) for p in picks]
            rarities_ = [get_song_rarity(p[1]) for p in picks]
            return picks, albums_, rarities_

        picks, albums, rarities = await asyncio.to_thread(_fetch_three)
        (song1, id_1, artist1), (song2, id_2, artist2), (song3, id_3, artist3) = picks
        album1, album2, album3 = albums

        songs = [song1, song2, song3]
        image_urls = [album1[2], album2[2], album3[2]]
        ids = [id_1, id_2, id_3]
        titles = [song1, song2, song3]
        artists = [artist1, artist2, artist3]
        variants = [draw_variant(), draw_variant(), draw_variant()]

        if "gutscookie" in variants and "mythic" not in variants:
            gutscookie_xp = 1500
            cookie_path = os.path.join(IMAGES_DIR, "gutscookie.png")
            file = discord.File(cookie_path, filename="gutscookie.png")
            embed = discord.Embed(
                title=f"🍪 **{ctx.author.display_name} pulled a GUTS COOKIE** 🍪",
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
            # Check if there's an active hunt
            hunt = get_mythic_hunt()
            if hunt:
                # Use the hunted song
                song_name = hunt['song_name']
                song_id = hunt['song_id']
                album_id = hunt['album_id']
                album_name = hunt['album_name']
                album_url = hunt['album_url']
                artist = hunt['artist']

                # Clear the hunt after use
                clear_mythic_hunt()

                log.info("HUNT TRIGGERED: %s by %s", song_name, artist)
            else:
                # Use the random mythic song
                index = variants.index("mythic")
                song_name = songs[index]
                song_id = ids[index]
                album = albums[index]
                album_id = album[0]
                album_name = album[1]
                album_url = album[2]
                artist = artists[index]
                log.info("Random mythic: %s by %s", song_name, artist)

            # Check if mythic can still be collected and user doesn't already own it
            if not can_collect_mythic(song_id) or user_owns_mythic(ctx.author.id, song_id):
                # Try to find another mythic that can still be collected and user doesn't own
                available_mythics = get_available_mythic_songs_for_user(ctx.author.id)

                if available_mythics:
                    # Pick a random available mythic
                    random_mythic = random.choice(available_mythics)
                    song_id, song_name, artist = random_mythic

                    # Get album info for this song
                    album = get_random_album(song_id)
                    album_id = album[0]
                    album_name = album[1]
                    album_url = album[2]

                    reason = "maxed out" if not can_collect_mythic(song_id) else "already owned"
                    log.info("Replaced %s mythic with: %s by %s", reason, song_name, artist)
                else:
                    # No available mythics left for this user, fall back to regular choice
                    log.info("No mythics available for this user, generating regular choices")
                    return await self.choice(ctx)

            # Get copy information
            copy_info = get_mythic_copy_info(song_id)
            next_copy_number = copy_info['next_copy_number']

            # Send congratulations message first
            # await ctx.send(f"*🎉 Congratulations {ctx.author.display_name}, you just pulled a mythic! 🎉*")

            embed = discord.Embed(
                title=f"💎 **{ctx.author.display_name} PULLED A MYTHIC SONG** 💎",
                description=f"||**{song_name}**|| ||**#{next_copy_number}**|| by ||**{artist}**|| from ||*{album_name}*||\n\n"
                           f"Click the button below to claim this mythic song!",
                color=discord.Color.from_rgb(140, 202, 247)
            )

            # Generate the mythic GIF in memory, off the event loop
            gif_buf = await asyncio.to_thread(generate_mythic_gif, album_url, True)

            # Create the claim view
            claim_view = MythicClaimView(ctx, song_id, album_id, song_name, artist)

            if gif_buf is not None:
                file = discord.File(gif_buf, filename="mythic_animated.gif", spoiler=True)
                embed.set_image(url="attachment://mythic_animated.gif")
                await ctx.send(embed=embed, file=file, view=claim_view)
            else:
                await ctx.send(embed=embed, content="(Missing mythic gif ❗)", view=claim_view)

            await self._maybe_sourpatch_bonus(ctx)
            return

        collage_buf = await asyncio.to_thread(
            make_3_song_collage, image_urls, titles, artists, variants, rarities, None, True
        )
        file = discord.File(collage_buf, filename="choices.jpg")

        embed = discord.Embed(
            title=f"🎶 **{ctx.author.display_name}'s choices**🎶",
            description="Click a button to pick your preferred song!",
            color=discord.Color.blurple()
        )
        embed.set_image(url="attachment://choices.jpg")

        await ctx.send(embed=embed, file=file, view=ChooseSongView(ctx, options=[(ids[i], titles[i], artists[i], albums[i], variants[i]) for i in range(3)]))

        await self._maybe_sourpatch_bonus(ctx)

    @choice.error
    async def choice_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Cool down! Try again in {round(error.retry_after)}s.")
            return

    @commands.command()
    @commands.has_role("bot_admin")
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
            description=f"The next mythic pull will be:\n**{song_name_corrected}** by **{artist}**\nfrom *{album_name_corrected}*",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=album_url)
        embed.set_footer(text="The next user to hit mythic will get this song!")

        await ctx.send(embed=embed)

    @commands.command(aliases=["clh"])
    @commands.has_role("bot_admin")
    async def clearhunt(self, ctx):
        """Clear the current mythic hunt"""
        old_hunt = get_mythic_hunt()
        if old_hunt:
            clear_mythic_hunt()
            await ctx.send(f"🚫 Cleared mythic hunt: **{old_hunt['song_name']}** by **{old_hunt['artist']}**")
        else:
            await ctx.send("❌ No active mythic hunt to clear.")

    @commands.command()
    @commands.has_role("bot_admin")
    async def checkhunt(self, ctx):
        """Check the current mythic hunt status"""
        hunt = get_mythic_hunt()
        if hunt:
            embed = discord.Embed(
                title="🎯 Current Mythic Hunt",
                description=f"**{hunt['song_name']}** by **{hunt['artist']}**\nfrom *{hunt['album_name']}*",
                color=discord.Color.gold()
            )
            embed.set_thumbnail(url=hunt['album_url'])
            embed.set_footer(text="This will be the next mythic pull!")
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ No active mythic hunt set.")

    @commands.command()
    @commands.has_role("bot_admin")
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
            description=f"**{song_name_corrected}** by **{artist}**\n"
                       f"from *{album_name_corrected}*",
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
    @commands.has_role("bot_admin")
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
            description=f"**{song_name_corrected}** by **{artist}**\n"
                       f"from *{album_name_corrected}*",
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
            await ctx.send("❌ You need the 'bot_admin' role to use this command!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing arguments!**\n"
                          "**Usage:** `.hunt <song_name> / <album_name> / <artist>`\n"
                          "**Example:** `.hunt Sports car / So Close To What / Tate McRae`")
        else:
            raise error

    @clearhunt.error
    async def clearhunt_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'bot_admin' role to use this command!")
        else:
            raise error

    @checkhunt.error
    async def checkhunt_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'bot_admin' role to use this command!")
        else:
            raise error

    @mythicstatus.error
    async def mythicstatus_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'bot_admin' role to use this command!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing arguments!**\n"
                          "**Usage:** `.mythicstatus <song_name> / <album_name> / <artist>`\n"
                          "**Example:** `.mythicstatus Sports car / So Close To What / Tate McRae`")
        else:
            raise error

    @testmythic.error
    async def testmythic_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'bot_admin' role to use this command!")
        else:
            raise error

async def setup(bot):
    await bot.add_cog(ChoiceCog(bot))
