import os, sys
import asyncio
import time
import random
import logging

from db import (add_song_to_collection, get_song_and_album_details,
                add_user_xp_many, add_vinyls_many, get_user_xp, get_mythic_copy_info,
                get_song_rarity, get_available_mythic_songs_for_user, claim_mythic,
                get_mythic_pull_state, get_mythic_owners,
                get_mythic_hunt, set_mythic_hunt, take_mythic_hunt)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
from utils.helpers import (get_random_songs, make_3_song_collage, get_random_album,
                           draw_variant, SOURPATCH_ID, SOURPATCH_IMAGE, IMAGES_DIR)
from utils.mystic import mythic_card_gif_bytes
import db
from utils import economy, odds
from utils import aesthetics
from utils.logsetup import event

log = logging.getLogger("grails.choice")

rarity_multiplier = odds.RARITY_XP_MULTIPLIER

variant_multiplier = odds.VARIANT_XP

def _collect_pick(user_id, song_id, album_id, variant):
    """Add a picked card and pay its XP, off the event loop in one hop.

    Returns (rarity, xp, xp_changes). A mythic never reaches this -- a drop
    with a mythic in it gets MythicClaimView instead of the three buttons.
    """
    rarity = get_song_rarity(song_id)
    xp = variant_multiplier.get(variant, 1) * rarity_multiplier.get(rarity, 1)
    add_song_to_collection(user_id, song_id=song_id, album_id=album_id, variant=variant)
    return rarity, xp, add_user_xp_many({user_id: xp})


def _claim_mythic_card(user_id, song_id, album_id, xp):
    """claim_mythic plus its XP, in one hop. Returns (status, copy_number, xp_changes)."""
    status, copy_number = claim_mythic(user_id, song_id, album_id)
    changes = add_user_xp_many({user_id: xp}) if status == "claimed" else {}
    return status, copy_number, changes


# ✅ View for button UI
class ChooseSongView(discord.ui.View):
    def __init__(self, ctx: commands.Context, options: list):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.options = options
        self.picked = False

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

        # Taken before the first await. The database work below now yields to
        # other handlers, so without this a second click landing meanwhile
        # would take a second card from the same drop.
        if self.picked:
            await interaction.response.send_message(
                "You already picked a song from this drop!", ephemeral=True)
            return
        self.picked = True
        try:
            rarity, added_xp, xp_changes = await asyncio.to_thread(
                _collect_pick, interaction.user.id, song_id, album_id, variant)
        except Exception:
            self.picked = False     # nothing was taken, so the drop stays open
            raise
        event(log, "pull selected", "%s by %s [%s/%s] -> %s's collection",
               song_name, artist, rarity, variant, interaction.user.name)

        # Check for level-up after adding XP
        xp_cog = interaction.client.get_cog('XPCog')
        if xp_cog:
            await xp_cog.announce_level_ups(xp_changes, interaction.message.channel,
                                            {interaction.user.id: interaction.user})

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

        # Marked before the first await, so a double-click cannot claim twice.
        # The cap, the one-copy rule and the copy number are settled in one
        # statement (db.claim_mythic); the number shown is the one it stored.
        self.claimed = True
        try:
            status, copy_number, xp_changes = await asyncio.to_thread(
                _claim_mythic_card, interaction.user.id, self.song_id, self.album_id,
                variant_multiplier.get("mythic", 1) * rarity_multiplier.get(self.rarity, 1))
        except Exception:
            self.claimed = False
            raise

        if status == "full":
            self.claimed = False
            await interaction.response.send_message(
                f"This mythic has reached its maximum number of copies "
                f"({odds.MYTHIC_MAX_COPIES}/{odds.MYTHIC_MAX_COPIES}) and can no longer be collected!",
                ephemeral=True)
            return
        if status == "owned":
            self.claimed = False
            await interaction.response.send_message("You already own a copy of this mythic song!", ephemeral=True)
            return

        # Check for level-up after adding XP
        xp_cog = interaction.client.get_cog('XPCog')
        if xp_cog:
            await xp_cog.announce_level_ups(xp_changes, interaction.message.channel,
                                            {interaction.user.id: interaction.user})

        # Disable the button
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
        # Read and cleared in one statement, so two mythic pulls landing
        # together cannot both be handed the hunted song.
        hunt = take_mythic_hunt()
        if hunt:
            song_name = hunt['song_name']
            song_id = hunt['song_id']
            album_id = hunt['album_id']
            album_name = hunt['album_name']
            album_url = hunt['album_url']
            artist = hunt['artist']
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
            # Drawn at random in the database: this used to fetch every
            # eligible song (~1,800 rows) to random.choice one of them.
            available = get_available_mythic_songs_for_user(user_id, limit=1)
            if not available:
                return None
            # Read off the state we already have -- this used to re-run the
            # copy-count query just to word a log line.
            reason = "maxed out" if not state['can_collect'] else "already owned"
            song_id, song_name, artist = available[0]
            album_id, album_name, album_url = get_random_album(song_id)
            state = get_mythic_pull_state(song_id, user_id)
            event(log, "mythic swapped", "%s -> %s by %s", reason, song_name, artist)

        next_copy_number = state['next_copy_number']

        # The rarity of whichever song is actually being awarded, not of the
        # drawn pick: the hunt branch never had a pick to read, and the swap
        # above can replace the song with one of a different tier.
        rarity = state['rarity']

    return song_id, song_name, artist, album_id, album_name, album_url, next_copy_number, rarity


def _mythic_report(song_name, album_name, artist, with_owners=True):
    """Song details, copy info and (optionally) owners for the admin mythic
    commands, in one thread hop. The details are all None when the song is
    not found."""
    details = get_song_and_album_details(song_name, album_name, artist)
    if not details[0]:
        return (*details, None, None)
    owners = get_mythic_owners(details[0]) if with_owners else None
    return (*details, get_mythic_copy_info(details[0]), owners)


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
            await asyncio.to_thread(add_vinyls_many, {ctx.author.id: (1, 0)})
            embed.description = (f"**{ctx.author.display_name}** got a "
                                 f"**free vinyl pull!** {aesthetics.named_emoji('vinyl')}")
            await ctx.send(embed=embed, file=file)
        else:
            user_xp = await asyncio.to_thread(get_user_xp, ctx.author.id)
            level = xp_cog.get_level_from_xp(user_xp)
            needed = xp_cog.get_xp_for_level(level + 1) - user_xp
            xp_changes = await xp_cog.add_xp({ctx.author.id: max(1, needed)})
            embed.description = f"**{ctx.author.display_name}** got **leveled up to the next level!** 🆙"
            await ctx.send(embed=embed, file=file)
            await xp_cog.announce_level_ups(xp_changes, ctx.channel, {ctx.author.id: ctx.author})

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
            
            # Add XP
            xp_changes = await asyncio.to_thread(add_user_xp_many, {ctx.author.id: gutscookie_xp})

            await ctx.send(embed=embed, file=file)

            # Check for level-up after sending the cookie message
            xp_cog = self.bot.get_cog('XPCog')
            if xp_cog:
                await xp_cog.announce_level_ups(xp_changes, ctx.channel, {ctx.author.id: ctx.author})

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
            description=f"||**#{next_copy_number}**|| ||**{song_name}**|| by ||**{artist}**|| from ||*{album_name}*||"
                        f"\n\n",
            # The rarity's own colour, matching the glow on the card art and
            # the pick embed. A fixed blue made an ultimate mythic and a basic
            # one look identical at the moment they land.
            color=discord.Color(aesthetics.rarity_colour_int(rarity)),
        )
        claim_view = MythicClaimView(ctx, song_id, album_id, song_name, artist, rarity)

        # Post the reveal and the claim button first. Rendering the animation
        # takes the better part of a second, and that used to be dead air before
        # anything appeared at all -- the player can now read the spoilers and
        # claim immediately while the GIF is edited in behind them.
        started = time.perf_counter()
        message = await ctx.send(embed=embed, view=claim_view)

        # Seeded on the song and the copy, so a given mythic always animates
        # the same way -- two people pulling #3 and #4 of the same song get
        # visibly different sparkle fields, and re-rendering either reproduces
        # it exactly.
        gif_buf = await asyncio.to_thread(
            mythic_card_gif_bytes, album_url,
            copy_number=next_copy_number, rarity=rarity,
            seed=f"{song_id}:{next_copy_number}")
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

    @commands.command(extras={"missing_arg": "❌ **Missing arguments!**\n"
                          "**Usage:** `.hunt <song_name> / <album_name> / <artist>`\n"
                          "**Example:** `.hunt Sports car / So Close To What / Tate McRae`"})
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
        song_id, song_name_corrected, album_id, album_name_corrected, album_url = await asyncio.to_thread(
            get_song_and_album_details, song_name, album_name, artist)

        if not song_id:
            await ctx.send(f"❌ **Song not found!**\n"
                          f"Could not find song `{song_name}` by `{artist}` in the database.")
            return

        if not album_id:
            await ctx.send(f"❌ **Album not found!**\n"
                          f"Could not find album `{album_name}` by `{artist}` in the database.")
            return

        # Store the hunt data
        await asyncio.to_thread(set_mythic_hunt, song_id, song_name_corrected, album_id,
                                album_name_corrected, album_url, artist)

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
        old_hunt = await asyncio.to_thread(take_mythic_hunt)
        if old_hunt:
            await ctx.send(f"🚫 Cleared mythic hunt: **{old_hunt['song_name']}** by "
                           f"**{old_hunt['artist']}** from *{old_hunt['album_name']}*")
        else:
            await ctx.send("❌ No active mythic hunt to clear.")

    @commands.command(aliases=["ckh"])
    @commands.has_role("grails-admin")
    async def checkhunt(self, ctx):
        """Check the current mythic hunt status"""
        hunt = await asyncio.to_thread(get_mythic_hunt)
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

    @commands.command(extras={"missing_arg": "❌ **Missing arguments!**\n"
                          "**Usage:** `.mythicstatus <song_name> / <album_name> / <artist>`\n"
                          "**Example:** `.mythicstatus Sports car / So Close To What / Tate McRae`"})
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

        # Look up the song, its copies and their owners in one hop
        song_id, song_name_corrected, album_id, album_name_corrected, album_url, copy_info, owners = \
            await asyncio.to_thread(_mythic_report, song_name, album_name, artist)

        if not song_id:
            await ctx.send(f"❌ **Song not found!**\n"
                          f"Could not find song `{song_name}` by `{artist}` in the database.")
            return

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
            if owners:
                # Each owner's stored copy number, not their place in a list:
                # a copy removed by an admin leaves a gap rather than
                # renumbering everyone after it.
                owner_list = []
                for user_id, collected_at, copy_number in owners:
                    owner_list.append(f"**#{copy_number}** - <@{user_id}>")

                embed.add_field(
                    name="Owners",
                    value="\n".join(owner_list),
                    inline=False
                )

        embed.set_thumbnail(url=album_url)
        await ctx.send(embed=embed)

    @commands.command(extras={"missing_arg": "❌ **Missing arguments!**\n"
                          "**Usage:** `.testmythic <song_name> / <album_name> / <artist>`\n"
                          "**Example:** `.testmythic Sports car / So Close To What / Tate McRae`"})
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

        # Look up the song and its copy information in one hop
        song_id, song_name_corrected, album_id, album_name_corrected, album_url, copy_info, _owners = \
            await asyncio.to_thread(_mythic_report, song_name, album_name, artist, False)

        if not song_id:
            await ctx.send(f"❌ **Song not found!**\n"
                          f"Could not find song `{song_name}` by `{artist}` in the database.")
            return

        next_copy = copy_info['next_copy_number']
        can_collect = copy_info['can_collect']

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



async def setup(bot):
    await bot.add_cog(ChoiceCog(bot))
