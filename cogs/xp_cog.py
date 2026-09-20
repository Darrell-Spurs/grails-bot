import discord
from discord import app_commands
from discord.ext import commands
import random, math, asyncio
from db import (get_user_xp, add_user_xp, user_exists, get_all_artists,
                can_claim_daily, update_daily_claim, add_song_to_collection,
                add_vinyl_count, get_vinyl_count, use_vinyl_pull,
                get_sig_vinyl_count, use_sig_vinyl_pull, add_sig_vinyl_count,
                get_top_users_by_xp, get_user_xp_rank)
from utils.helpers import (pick_unique_song,
                           SOURPATCH_ID, SOURPATCH_IMAGE)
from utils import aesthetics, economy, odds
from utils.errors import report_unhandled
from utils.vinyl import vinyl_gif_sig_art_from_url, vinyl_static_bytes
import logging
from utils.command_types import slash_only
from utils.aesthetics import named_emoji, rarity_emoji, variant_emoji, card_emoji

log = logging.getLogger("grails.xp")

NL = chr(10)


class XPCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        # Store user levels to track level-ups {user_id: last_known_level}
        self.user_levels = {}
        self.multiplier = 500  # XP multiplier for level calculations

    def get_level_from_xp(self, xp):
        """Calculate level based on XP with cap at 8000 XP per level after level 15"""
        # Calculate XP needed for level 15
        level_15_xp = (1 + 15) * 15 * self.multiplier // 2  # 60,000 XP
        
        if xp <= level_15_xp:
            # Use quadratic formula for levels 1-15: level² + level - (2 * xp / multiplier) = 0
            # level = (-1 + √(1 + 8 * xp / multiplier)) / 2
            discriminant = 1 + 8 * xp / self.multiplier
            level = math.floor((-1 + math.sqrt(discriminant)) / 2)
        else:
            # For XP beyond level 15, each additional 8000 XP = 1 level
            excess_xp = xp - level_15_xp
            additional_levels = math.floor(excess_xp / 8000)
            level = 15 + additional_levels

        log.debug("xp=%s level=%s", xp, level)
        return level

    def get_xp_for_level(self, level):
        """Calculate XP needed to reach a specific level with cap at 8000 XP after level 15"""
        if level <= 15:
            # Use quadratic formula for levels 1-15: xp = (level² + level) * multiplier / 2
            return (1 + level) * level * self.multiplier // 2
        else:
            # For levels 16+, each level requires exactly 8000 XP
            # Calculate XP for level 15, then add 8000 for each additional level
            level_15_xp = (1 + 15) * 15 * self.multiplier // 2  # 60,000 XP for level 15
            additional_levels = level - 15
            return level_15_xp + (additional_levels * 8000)

    def get_xp_to_next_level(self, current_xp):
        """Calculate XP needed to reach next level"""
        current_level = self.get_level_from_xp(current_xp)
        xp_for_next_level = self.get_xp_for_level(current_level + 1)
        return xp_for_next_level - current_xp

    def xp_progress_text(self, current_xp):
        """The level, the bar and the gap to the next level, as embed text.

        Returned as a description rather than a field: a field always renders
        its name on a line of its own, and even an invisible name leaves that
        gap behind. A description sits straight under the author line.

        Level 0 is included deliberately. This used to be skipped entirely,
        so a brand-new player opened /xp to an empty embed -- yet "Level 0,
        500 XP to go" is exactly what they came to see.
        """
        level = self.get_level_from_xp(current_xp)
        floor = self.get_xp_for_level(level)
        ceiling = self.get_xp_for_level(level + 1)
        span = max(ceiling - floor, 1)
        into = current_xp - floor

        bar_length = 20
        filled = int(into / span * bar_length)
        bar = "█" * filled + "░" * (bar_length - filled)
        return (f"**Level {level}**\n\n`{bar}` {into:,}/{span:,} XP\n\n"
                f"`{ceiling - current_xp:,}` XP needed to the next level")

    def get_level_rewards(self, level):
        """Get reward description for a specific level"""
        rewards = {
            1: "🎵 1x Vinyl Pull + Welcome Badge",
            2: "🎵 2x Vinyl Pulls + Bronze Collector Badge",
            3: "🎵 3x Vinyl Pulls + Music Explorer Badge",
            4: "🎵 4x Vinyl Pulls + Rhythm Master Badge",
            5: "🎵 5x Vinyl Pulls + Silver Collector Badge + Special Title",
            10: "🎵 10x Vinyl Pulls + Gold Collector Badge + Exclusive Avatar",
            15: "🎵 15x Vinyl Pulls + Platinum Collector Badge + Custom Role",
            20: "🎵 20x Vinyl Pulls + Diamond Collector Badge + VIP Access",
            25: "🎵 25x Vinyl Pulls + Legendary Collector Badge + All Perks"
        }

    async def check_level_up(self, user_id, level_checkpoint, channel, user=None):
        xp_ = get_user_xp(user_id)
        current_level = self.get_level_from_xp(xp_)      
        # Check if they leveled up
        if current_level > level_checkpoint:
            log.info("level up detected for %s", user_id)
            # Calculate how many levels they gained
            levels_gained = current_level - level_checkpoint

            # Add vinyl pulls for each level gained
            add_vinyl_count(user_id, levels_gained)
            if not current_level % 5:
                add_sig_vinyl_count(user_id, 1)

            # Send level-up message
            if not user:
                try:
                    user = await self.bot.fetch_user(user_id)
                except discord.HTTPException:
                    user = self.bot.get_user(user_id)

            level_up_note = f"{levels_gained} {named_emoji('vinyl')} {'s' if levels_gained > 1 else ''}"
            if not current_level % 5:
                level_up_note = f" 1 {named_emoji('sig_vinyl')} & " + level_up_note
            level_up_note = "You received " + level_up_note
            remaining_note = f"Owned: {get_vinyl_count(user_id)} {named_emoji('vinyl')}, {get_sig_vinyl_count(user_id)} {named_emoji('sig_vinyl')}"
            
            if user:
                embed = discord.Embed(
                    title="🎉 LEVEL UP!",
                    description=f"**{user.display_name}** reached **Level {current_level}**!\n {level_up_note}\n {remaining_note}",
                    color=discord.Color.gold()
                )

                total_vinyl_count = get_vinyl_count(user_id)
                total_sig_vinyl_count = get_sig_vinyl_count(user_id)

                embed.set_thumbnail(url=user.display_avatar.url)
                embed.set_footer(text=f"Use .v and .sv to open your vinyls")

                await channel.send(embed=embed)
            else:
                log.warning("Could not find user %s for level-up message", user_id)
  
    @commands.hybrid_command(description="Check your XP or another user's XP")
    @slash_only()
    async def xp(self, ctx, user: discord.User = None):
        """Check your current XP or another user's XP
        Usage: .xp [@user]
        Example: .xp or .xp @username
        """
        target_user = user or ctx.author

        # Check if user is registered
        if not user_exists(target_user.id):
            if target_user == ctx.author:
                await ctx.send(
                    "❌ **You are not registered!** Use `.register` to get started."
                )
            else:
                await ctx.send(
                    f"❌ **{target_user.display_name}** is not registered!")
            return

        current_xp = get_user_xp(target_user.id)
        current_level = self.get_level_from_xp(current_xp)

        embed = discord.Embed(
            color=discord.Color.gold(),
            description=self.xp_progress_text(current_xp),
        )

        embed.set_author(name=target_user.display_name, icon_url=target_user.display_avatar.url)
        embed.set_footer(text="Use /xphelp to learn more about how to get XP!")

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="top", description="The XP leaderboard")
    @slash_only()
    @app_commands.describe(count="How many players to show (1-25, default 10)")
    async def top(self, ctx, count: int = 10):
        """Show the highest-XP players.

        Your own standing is appended when you are not already on the page, so
        the command answers "where am I?" as well as "who is winning?".
        """
        await ctx.defer()
        count = max(1, min(count, 25))

        rows = await asyncio.to_thread(get_top_users_by_xp, count)
        if not rows:
            await ctx.send("Nobody has registered yet — be the first with `/register`.")
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for place, (user_id, username, xp) in enumerate(rows, start=1):
            # The stored username can be stale; the live member name wins when
            # the user is still in the server.
            member = ctx.guild.get_member(int(user_id)) if ctx.guild else None
            name = member.display_name if member else (username or f"User {user_id}")
            marker = medals.get(place, f"`#{place:>2}`")
            you = "  ←  you" if str(user_id) == str(ctx.author.id) else ""
            lines.append(f"{marker} **{name}** — `{xp:,}` XP · Lv `{self.get_level_from_xp(xp)}`{you}")

        embed = discord.Embed(
            title="🏆 XP leaderboard",
            description=NL.join(lines),
            colour=discord.Colour(aesthetics.ACCENT_COLOR),
        )

        shown = {str(r[0]) for r in rows}
        if str(ctx.author.id) not in shown:
            rank = await asyncio.to_thread(get_user_xp_rank, ctx.author.id)
            if rank is None:
                embed.set_footer(text="You are not registered yet — run /register to join the board.")
            else:
                my_xp = await asyncio.to_thread(get_user_xp, ctx.author.id)
                embed.add_field(
                    name="Your standing",
                    value=f"`#{rank}` — `{my_xp:,}` XP · Lv `{self.get_level_from_xp(my_xp)}`",
                    inline=False,
                )
        await ctx.send(embed=embed)

    @top.error
    async def top_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send("`count` has to be a number between 1 and 25.")
            return
        await report_unhandled(log, ctx, error, command="top")

    @commands.hybrid_command(name="xphelp", description="How XP, levels and vinyl rewards work")
    @slash_only()
    async def xphelp(self, ctx):
        """Explain how XP, levelling and vinyl rewards work"""
        embed = discord.Embed(
            title="How XP works",
            description="Claim a card and you earn its **Base XP** x **Rarity Multiplier**.",
            color=discord.Color.teal())

        # Both rows are generated from utils/odds.py rather than written out, so
        # the help can never quote numbers the game does not actually pay. It
        # used to promise 10/50/200/800 against a real 30/100/500/1200.
        base = " · ".join(
            f"{variant_emoji(v)} **{xp:,}**" for v, xp in odds.VARIANT_XP.items())
        mult = " · ".join(
            f"{rarity_emoji(r)} **x{m:g}**"
            for r, m in sorted(odds.RARITY_XP_MULTIPLIER.items(),
                               key=lambda kv: -kv[1]))

        embed.add_field(name="Base XP by variant", value=base, inline=False)
        embed.add_field(name="Rarity multiplier", value=mult, inline=False)

        embed.add_field(
            name="Other ways to earn",
            value=f"Daily reward **{odds.DAILY_XP_MIN:,} - {odds.DAILY_XP_MAX:,}**XP with `.daily`\n"
                  f"Events may be *coming soon* 👀\n",
            inline=False)

        embed.add_field(
            name="Levelling",
            value="Each level costs **500 XP** more than the last level.\n"
                  f"Leveling up gives you 1 vinyl {variant_emoji('vinyl')} pull.\n"
                  f"Gain a signature vinyl {variant_emoji('sig_vinyl')} every 5 levels.\n",
            inline=False)

        embed.set_footer(text="/xp for your progress · /vinylcheck for your vinyl inventory")
        await ctx.send(embed=embed)


    @commands.hybrid_command(description="Claim your daily XP reward")
    async def daily(self, ctx):
        """Claim your daily XP reward"""
        # Check if user is registered
        if not user_exists(ctx.author.id):
            await ctx.send(
                "❌ **You are not registered!** Use `.register` to get started."
            )
            return

        # Check if user can claim daily reward
        can_claim, remaining_seconds = can_claim_daily(ctx.author.id)

        if not can_claim:
            # Second person, and no name: for a slash command Discord already
            # prints "<user> used /daily" above the reply, and a prefix reply
            # is attached to their message -- so the sentence does not have to
            # carry the identity, and reads naturally instead.
            #
            # Plain text on purpose: a "come back later" note is not an event,
            # and an embed would give it the weight of one.
            await ctx.send(
                f"You've already claimed today's reward "
                f"— come back {economy.discord_countdown(remaining_seconds)}.")
            return

        # Add daily XP reward and update claim timestamp
        daily_xp = random.randint(10, 100) * 10

        level_checkpoint = self.get_level_from_xp(get_user_xp(ctx.author.id))
        add_user_xp(ctx.author.id, daily_xp)
        update_daily_claim(ctx.author.id)

        # The claim itself is a reward, and the one thing a player does every
        # day, so it gets an embed: the XP as the headline, their standing
        # underneath, and a live countdown to the next one.
        embed = discord.Embed(
            title=f"You got +{daily_xp:,} XP from daily rewards!",
            description=self.xp_progress_text(get_user_xp(ctx.author.id)),
            colour=discord.Color.gold(),
        )
        embed.set_author(name=ctx.author.display_name,
                         icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

        # Check for level-up after claiming daily reward
        await self.check_level_up(ctx.author.id, level_checkpoint, ctx.channel, ctx.author)

    @commands.hybrid_command(aliases=['vc'], description="Check available vinyl pulls")
    async def vinylcheck(self, ctx, user: discord.User = None):
        """Check how many vinyl pulls you or another user has available
        Usage: .vinylcheck [@user]
        Example: .vinylcheck or .vinylcheck @username
        """
        target_user = user or ctx.author
        
        # Check if user is registered
        if not user_exists(target_user.id):
            if target_user == ctx.author:
                await ctx.send(
                    "❌ **You are not registered!** Use `.register` to get started."
                )
            else:
                await ctx.send(
                    f"❌ **{target_user.display_name}** is not registered!")
            return
        
        vinyl_count = get_vinyl_count(target_user.id)
        sig_count = get_sig_vinyl_count(target_user.id)

        # Both counts in the description, not as fields: the phone app stacks
        # every field on its own row, so two lines of text look the same
        # everywhere. The name rides in the author line, which also leaves the
        # thumbnail slot free rather than duplicating the avatar.
        embed = discord.Embed(
            title="Vinyl Inventory",
            description=(f"{named_emoji('sig_vinyl')} Signature vinyl: **{sig_count}**\n"
                         f"{named_emoji('vinyl')} Vinyl: **{vinyl_count}**"),
            color=discord.Color.from_rgb(139, 69, 19),
        )
        embed.set_author(name=target_user.display_name,
                         icon_url=target_user.display_avatar.url)
        embed.set_footer(text="Open them with .sv / .v")
        await ctx.send(embed=embed)

    async def _do_vinyl_pull(self, ctx, guaranteed_sig):
        """Shared implementation for .vinyl and .sigvinyl (they were ~90% identical)."""
        if not user_exists(ctx.author.id):
            await ctx.send("❌ **You are not registered!** Use `.register` to get started.")
            return

        # Pull-count check + consume
        if guaranteed_sig:
            if get_sig_vinyl_count(ctx.author.id) <= 0:
                await ctx.send("❌ **No signature vinyl pulls available.** Level up to collect more!")
                return
            if not use_sig_vinyl_pull(ctx.author.id):
                await ctx.send("❌ **Error using signature vinyl pull!** Please try again.")
                return
        else:
            if get_vinyl_count(ctx.author.id) <= 0:
                await ctx.send("❌ **No vinyl pulls available.** Level up to collect more!")
                return
            if not use_vinyl_pull(ctx.author.id):
                await ctx.send("❌ **Error using vinyl pull!** Please try again.")
                return

        if not get_all_artists():
            await ctx.send("❌ **No artists found in the database!** Add some artists first.")
            return

        is_sig = True if guaranteed_sig else (random.random() < odds.SIG_VINYL_CHANCE)
        dedup_variant = "sig_vinyl" if guaranteed_sig else "vinyl"
        weight_variant = "sig_vinyl" if is_sig else ""

        # Pick a (non-duplicate) song; all DB work runs off the event loop.
        song, song_id, artist, album, rarity = await asyncio.to_thread(
            pick_unique_song, str(ctx.author.id), dedup_variant, weight_variant
        )

        # Sour Patch Kids fallback (empty-catalog safeguard): refund and bail nicely.
        if song_id == SOURPATCH_ID:
            (add_sig_vinyl_count if guaranteed_sig else add_vinyl_count)(ctx.author.id, 1)
            await ctx.send(
                content="😋 The vinyl machine spat out a **Sour Patch Kids**! Your pull was refunded — try again.",
                file=discord.File(SOURPATCH_IMAGE, filename="sourpatchkids.png"))
            return

        album_id = album[0]
        image_url = album[2]

        # Generate media off the event loop: static PNG for normal vinyl, animated GIF for sig.
        if is_sig:
            media_buf = await asyncio.to_thread(
                vinyl_gif_sig_art_from_url, image_url, artist,
                output_size=500, rarity=rarity, num_frames=40, duration=80, return_bytes=True)
            media_name = "spinning_vinyl_sig.gif"
        else:
            media_buf = await asyncio.to_thread(
                vinyl_static_bytes, image_url, output_size=500, rarity=rarity)
            media_name = "spinning_vinyl.png"

        emoji = card_emoji(rarity, variant="sig_vinyl" if is_sig else "vinyl")  # Preload the emoji for faster rendering
        if is_sig:
            title = f"{named_emoji('sig_vinyl')} {ctx.author.display_name} received a Signature Vinyl!"
        else:
            title = f"{named_emoji('vinyl')} {ctx.author.display_name} received a Vinyl!"
        embed = discord.Embed(title=title, color=discord.Color.from_rgb(139, 69, 19))


        # The song goes in the field *value*: Discord does not render markdown
        # in a field name, so the asterisks showed through literally.
        owned = get_sig_vinyl_count(ctx.author.id) if guaranteed_sig else get_vinyl_count(ctx.author.id)
        remaining = named_emoji('sig_vinyl') if guaranteed_sig else named_emoji('vinyl')
        embed.add_field(
            name=f"{emoji} **{song}** by **{artist}** from *{album[1]}*\n",
            value=f"Owned: {owned} {remaining}",
            inline=False)

        embed.set_image(url=f"attachment://{media_name}")
        file = discord.File(media_buf, filename=media_name, spoiler=False)

        add_song_to_collection(ctx.author.id, song_id, album_id,
                               variant=("sig_vinyl" if is_sig else "vinyl"))
        await ctx.send(embed=embed, file=file)

    @commands.command(aliases=['sv'], description="Pull a guaranteed signature vinyl song")
    async def sigvinyl(self, ctx):
        """Pull a guaranteed signature vinyl rarity song"""
        await self._do_vinyl_pull(ctx, guaranteed_sig=True)

    @commands.command(aliases=['v'], description="Pull a vinyl song from your rewards")
    async def vinyl(self, ctx):
        """Pull a vinyl rarity song from rewards"""
        await self._do_vinyl_pull(ctx, guaranteed_sig=False)

    @vinyl.error
    async def vinyl_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Cool down! Try again in {round(error.retry_after)}s.")
            return
        # This used to catch commands.CommandError, which covers a wrapped
        # NameError just as happily as a user mistake -- so every real bug in
        # .vinyl showed the same friendly text and logged nothing.
        await report_unhandled(
            log, ctx, error, command="vinyl",
            user_message="❌ **Error pulling vinyl song!** It has been logged — please try again.")

    @xp.error
    async def xp_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send("❌ **Invalid user!** Please mention a valid user.")
            return
        await report_unhandled(log, ctx, error, command="xp")


    @commands.command(aliases=['gv'])
    @commands.has_role("grails-admin")
    async def givevinyl(self, ctx, user: discord.User, amount: int = 1):
        """Give vinyl pulls to a user (bot-admin only)
        Usage: .givevinyl <user> [amount]
        Example: .givevinyl @username 5
        """
        # Check if target user is registered
        if not user_exists(user.id):
            await ctx.send(
                f"❌ **{user.display_name}** is not registered! They need to use `.register` first."
            )
            return
        
        # Validate amount
        if abs(amount) > 10000:
            await ctx.send("❌ **Invalid amount!** Please provide a number between -10000-10000.")
            return
        
        # Add vinyl pulls to the user
        add_vinyl_count(user.id, amount)
        
        # Get updated vinyl count
        total_vinyl_count = get_vinyl_count(user.id)
        
        # Create success embed
        embed = discord.Embed(
            title="🎁 Vinyl Pulls Granted!",
            description=f"**{user.display_name}** received +**{amount}** {named_emoji('vinyl')}{'s' if amount > 1 else ''}!\nOwned: {total_vinyl_count} {named_emoji('vinyl')}",
            color=discord.Color.green()
        )
              
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="Use .vinyl to claim vinyl songs!")
        
        await ctx.send(embed=embed)
        
        # Try to DM the user about the gift (optional, won't fail if DMs are closed)
        try:
            dm_embed = discord.Embed(
                title="🎁 You received vinyl pulls!",
                description=f"A bot admin granted you **{amount}** {named_emoji('vinyl')} ! \n \
                            Owned: {total_vinyl_count} {named_emoji('vinyl')}",
                color=discord.Color.green()
            )
            dm_embed.set_footer(text="Use .vinyl in the server to claim your songs!")
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            # If DM fails (e.g. DMs closed), that's okay - the channel message was sent
            pass

    @givevinyl.error
    async def givevinyl_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing user!**\n"
                           "**Usage:** `.givevinyl <user> [amount]`\n"
                           "**Example:** `.givevinyl @username 5`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("❌ **Invalid user or amount!** Please mention a valid user and provide a valid number.")
        elif isinstance(error, commands.MissingRole):
            await ctx.send("❌ **Permission denied!** This command requires the `bot-admin` role.")
        else:
            await report_unhandled(log, ctx, error, command="givevinyl")

    @commands.command(aliases=['gsv'])
    @commands.has_role("grails-admin")
    async def give_sigvinyl(self, ctx, user: discord.User, amount: int = 1):
        """Give signature vinyl pulls to a user (grails-admin only)
        Usage: .give_sigvinyl <user> [amount]
        Example: .give_sigvinyl @username 5
        """
        # Check if target user is registered
        if not user_exists(user.id):
            await ctx.send(
                f"❌ **{user.display_name}** is not registered! They need to use `.register` first."
            )
            return
        
        # Validate amount
        if abs(amount) > 10000:
            await ctx.send("❌ **Invalid amount!** Please provide a number between -10000-10000.")
            return

        # Add signature vinyl pulls to the user
        add_sig_vinyl_count(user.id, amount)
        
        # Get updated vinyl count
        total_sig_vinyl_count = get_sig_vinyl_count(user.id)

        # Create success embed
        embed = discord.Embed(
            title="🎁 Signature Vinyl Pulls Granted!",
            description=f"**{user.display_name}** received +**{amount}** {named_emoji('sig_vinyl')}{'s' if amount > 1 else ''}!\nOwned: {total_sig_vinyl_count} {named_emoji('sig_vinyl')}",
            color=discord.Color.green()
        )
        
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="Use .sigvinyl to claim signature vinyl songs!")

        await ctx.send(embed=embed)
        
        # Try to DM the user about the gift (optional, won't fail if DMs are closed)
        try:
            dm_embed = discord.Embed(
                title="🎁 You received signature vinyl pulls!",
                description=f"A bot admin granted you **{amount}** {named_emoji('sig_vinyl')}{'s' if amount > 1 else ''}!\n \
                            Owned: {total_sig_vinyl_count} {named_emoji('sig_vinyl')}",
                color=discord.Color.green()
            )
            dm_embed.set_footer(text="Use .sv in the server to claim your songs!")
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            # If DM fails (e.g. DMs closed), that's okay - the channel message was sent
            pass

    @give_sigvinyl.error
    async def give_sigvinyl_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing user!**\n"
                           "**Usage:** `.give_sigvinyl <user> [amount]`\n"
                           "**Example:** `.give_sigvinyl @username 5`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("❌ **Invalid user or amount!** Please mention a valid user and provide a valid number.")
        elif isinstance(error, commands.MissingRole):
            await ctx.send("❌ **Permission denied!** This command requires the `bot-admin` role.")
        else:
            await report_unhandled(log, ctx, error, command="give_sigvinyl")


async def setup(bot):
    await bot.add_cog(XPCog(bot))
