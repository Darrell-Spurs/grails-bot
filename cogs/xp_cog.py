import discord
from discord.ext import commands
import random, math, asyncio
from db import (get_user_xp, add_user_xp, user_exists, get_all_artists,
                can_claim_daily, update_daily_claim, get_song_rarity, add_song_to_collection,
                get_user_song_by_details, add_vinyl_count, get_vinyl_count, use_vinyl_pull,
                get_sig_vinyl_count, use_sig_vinyl_pull, add_sig_vinyl_count)
from utils.helpers import (get_random_song, get_random_album, pick_unique_song,
                           SOURPATCH_ID, SOURPATCH_IMAGE)
from utils.vinyl import vinyl_gif_art_from_url, vinyl_gif_sig_art_from_url, vinyl_static_bytes
import logging

log = logging.getLogger("grails.xp")


class XPCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        # Store claimed rewards per user {user_id: [list_of_claimed_levels]}
        self.claimed_rewards = {}
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

        # Default reward for levels not specifically defined
        if level not in rewards:
            if level < 5:
                return f"🎵 {level}x Vinyl Pulls + Progress Badge"
            elif level < 10:
                return f"🎵 {level}x Vinyl Pulls + Advanced Badge"
            elif level < 20:
                return f"🎵 {level}x Vinyl Pulls + Master Badge + Special Perk"
            else:
                return f"🎵 {level}x Vinyl Pulls + Elite Badge + Premium Perks"

        return rewards[level]

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
            
            if user:
                embed = discord.Embed(
                    title="🎉 LEVEL UP! 🎉",
                    description=f"**{user.display_name}** reached **Level {current_level}**!",
                    color=discord.Color.gold()
                )
                
                level_up_note = f"+{levels_gained} vinyl pull{'s' if levels_gained > 1 else ''}"
                if not current_level % 5:
                    level_up_note = f"+1 signature vinyl pull, " + level_up_note
                embed.add_field(
                    name="🎵 Vinyl Pulls Earned",
                    value=level_up_note,
                    inline=True
                )
                
                total_vinyl_count = get_vinyl_count(user_id)
                total_sig_vinyl_count = get_sig_vinyl_count(user_id)

                embed.add_field(
                    name="💽 Total Vinyl Pulls",
                    value=f"{total_sig_vinyl_count} signature{'s' if total_sig_vinyl_count != 1 else ''}, {total_vinyl_count} vinyl{'s' if total_vinyl_count != 1 else ''} available",
                    inline=True
                )
                
                embed.set_thumbnail(url=user.display_avatar.url)
                embed.set_footer(text="Use **.vinyl/.sigvinyl** to claim your vinyl songs!")

                await channel.send(embed=embed)
            else:
                log.warning("Could not find user %s for level-up message", user_id)
  
    @commands.hybrid_command(description="Check your XP or another user's XP")
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
        xp_to_next = self.get_xp_to_next_level(current_xp)

        embed = discord.Embed(
            title=f"🌟 XP Stats for {target_user.display_name}",
            color=discord.Color.gold())

        embed.add_field(name="🧬 Current XP",
                        value=f"`{current_xp:,}` XP",
                        inline=True)
        embed.add_field(name="📊 Current Level",
                        value=f"Level `{current_level}`",
                        inline=True)
        embed.add_field(name="🔬 XP to Next Level",
                        value=f"`{xp_to_next:,}` XP needed",
                        inline=True)

        # Progress bar
        if current_level > 0:
            # xp_floor = int(current_level ** 2 * self.multiplier / 2)
            xp_for_next = self.get_xp_for_level(current_level + 1)
            xp_floor = self.get_xp_for_level(current_level)
            current_xp = get_user_xp(target_user.id)
            xp_in_current_level = current_xp - xp_floor

            progress = xp_in_current_level / (xp_for_next - xp_floor)
            bar_length = 20
            filled = int(progress * bar_length)
            bar = "█" * filled + "░" * (bar_length - filled)
            embed.add_field(name="📈 Level Progress",
                            value=f"`{bar}` {xp_in_current_level}/{xp_for_next - xp_floor} XP",
                            inline=False)

        embed.set_thumbnail(url=target_user.display_avatar.url)
        embed.set_footer(text="Keep collecting music to earn more XP!")

        await ctx.send(embed=embed)

    @commands.command()
    @commands.has_role("no_one")
    async def level(self, ctx, user: discord.User = None):
        """Check your current level and level progression
        Usage: .level [@user]
        Example: .level or .level @username
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
            title=f"🏆 Level Info for {target_user.display_name}",
            description=
            f"**Current Level:** `{current_level}`\n**Total XP:** `{current_xp:,}`",
            color=discord.Color.gold())

        # Show next few level requirements
        level_info = ""
        for i in range(max(1, current_level), min(current_level + 6, 26)):
            xp_needed = self.get_xp_for_level(i)
            if i <= current_level:
                level_info += f"✅ Level {i}: {xp_needed:,} XP\n"
            else:
                level_info += f"🔒 Level {i}: {xp_needed:,} XP\n"

        embed.add_field(name="📊 Level Requirements",
                        value=level_info,
                        inline=False)

        if current_level > 0:
            embed.add_field(name="🎁 Current Level Reward",
                            value=self.get_level_rewards(current_level),
                            inline=False)

        embed.set_thumbnail(url=target_user.display_avatar.url)
        embed.set_footer(
            text="Each level requires 1000 more XP than the previous!")

        await ctx.send(embed=embed)

    @commands.command(aliases=['re'])
    async def rewards(self, ctx):
        """Check rewards for reaching different levels"""
        embed = discord.Embed(
            title="🎁 Level Rewards",
            description="Here are the rewards you can earn by leveling up!",
            color=discord.Color.teal())
        # Show rewards for key levels
        # key_levels = [1, 2, 3, 5, 10, 15, 20, 25]
        # for level in key_levels:
        #     reward = self.get_level_rewards(level)
        #     xp_needed = self.get_xp_for_level(level)
        #     embed.add_field(
        #         name=f"Level {level} ({xp_needed:,} XP)",
        #         value=reward,
        #         inline=False
        #     )

        embed.add_field(
            name="🎉 Level Rewards Overview",
            value="- Each level requires 500 more XP than the previous one.\n"
                  "- You earns a vinyl pull for every level you reach!\n"
                  "- Tere are also special rewards on certain levels *(coming soon)*"
        )

        embed.add_field(
            name="💡 How to Earn XP",
            value=
            "- ⚪️ Claim a default song: **10xp** \n" \
            "- 🧩 Claim a glitched song: **50xp**\n" \
            "- ✏️ Claim a sketch song: **200xp**\n" \
            "- 💎 Claim a mythic song: **800xp**\n" \
            "- 📅 Claim a daily reward: **100-1000xp**\n" \
            "- 🚩 Participate in events: **Coming Soon!**",
            inline=False
        )

        embed.add_field(
            name="🔍 Rarity Multipliers",
            value=
            "You get a higher XP reward for claiming rarer songs:\n"
            "- 🪐 Ultimate: **x3** \n" \
            "- 🏆 Legendary: **x2.5**\n" \
            "- ⭐ Elite: **x2**\n" \
            "- 🔮 Unique: **x1.5**\n" \
            "- 📄 Basic: **x1**\n",
            inline=False
        )
        
        embed.set_footer(
            text="Use .vinyl to claim your vinyls \nUse .vinylcheck to check your remaining vinyls claims!")
        await ctx.send(embed=embed)

    @commands.command(aliases=['cr'])
    async def claimreward(self, ctx, level: int):
        """Claim the reward for reaching a specific level
        Usage: .claimreward <level>
        Example: .claimreward 5
        """
        # Check if user is registered
        if not user_exists(ctx.author.id):
            await ctx.send(
                "❌ **You are not registered!** Use `.register` to get started."
            )
            return

        if level < 1 or level > 25:
            await ctx.send(
                "❌ **Invalid level!** You can only claim rewards for levels 1-25."
            )
            return

        current_xp = get_user_xp(ctx.author.id)
        current_level = self.get_level_from_xp(current_xp)

        # Check if user has reached the level
        if current_level < level:
            xp_needed = self.get_xp_for_level(level)
            xp_remaining = xp_needed - current_xp
            await ctx.send(
                f"❌ **You haven't reached level {level} yet!**\n"
                f"You need `{xp_remaining:,}` more XP to reach this level.")
            return

        # Initialize user's claimed rewards if not exists
        if ctx.author.id not in self.claimed_rewards:
            self.claimed_rewards[ctx.author.id] = []

        # Check if reward already claimed
        if level in self.claimed_rewards[ctx.author.id]:
            await ctx.send(
                f"❌ **You have already claimed the reward for level {level}!**"
            )
            return

        # Claim the reward
        self.claimed_rewards[ctx.author.id].append(level)
        reward_description = self.get_level_rewards(level)

        embed = discord.Embed(
            title="🎉 Reward Claimed!",
            description=
            f"**{ctx.author.display_name}** claimed the reward for **Level {level}**!",
            color=discord.Color.green())

        embed.add_field(name="🎁 Reward Received",
                        value=reward_description,
                        inline=False)

        # If reward includes vinyl pulls, mention they can use .vinyl command
        if "Vinyl Pull" in reward_description:
            vinyl_count = level  # Number of vinyl pulls equals the level
            embed.add_field(
                name="🎵 Vinyl Pulls Available",
                value=
                f"You now have `{vinyl_count}` vinyl pulls available!\nUse `.vinyl` to pull rare songs!",
                inline=False)

        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.set_footer(text="Congratulations on reaching this milestone!")

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
            # Calculate hours and minutes remaining
            hours = int(remaining_seconds // 3600)
            minutes = int((remaining_seconds % 3600) // 60)

            time_left = ""
            if hours > 0:
                time_left += f"{hours}h "
            if minutes > 0:
                time_left += f"{minutes}m"
            if not time_left:
                time_left = "less than 1m"

            await ctx.send(
                f"⏰ **Daily reward already claimed!**\n"
                f"You can claim your next daily reward in **{time_left}**.")
            return

        # Add daily XP reward and update claim timestamp
        daily_xp = random.randint(10, 100) * 10

        level_checkpoint = self.get_level_from_xp(get_user_xp(ctx.author.id))
        add_user_xp(ctx.author.id, daily_xp)
        update_daily_claim(ctx.author.id)

        await ctx.send(f"✅ You claimed your daily reward of **{daily_xp} XP!**")
        
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
        current_level = self.get_level_from_xp(get_user_xp(target_user.id))
        
        embed = discord.Embed(
            title=f"💽 {target_user.display_name}'s Vinyl Status",
            color=discord.Color.from_rgb(139, 69, 19)
        )
        
        embed.add_field(
            name="📀 Available Vinyl Pulls",
            value=f"**{vinyl_count}** pulls remaining",
            inline=True
        )
        
        # embed.add_field(
        #     name="📊 Current Level",
        #     value=f"Level **{current_level}**",
        #     inline=True
        # )
        
        if vinyl_count > 0:
            embed.add_field(
                name="💡 How to Use",
                value="Use `.vinyl` to claim vinyl songs!",
                inline=False
            )
        else:
            embed.add_field(
                name="📈 Earn More",
                value="Level up to earn more vinyl pulls!\nEach level gives you +1 vinyl pull.",
                inline=False
            )
        
        embed.set_thumbnail(url=target_user.display_avatar.url)
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

        is_sig = True if guaranteed_sig else (random.random() < 0.1)
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

        if is_sig:
            title = f"🖋️ {ctx.author.display_name} Received {'a ' if guaranteed_sig else ''}Signature Vinyl!"
        else:
            title = f"💽 {ctx.author.display_name} Received Vinyl!"
        embed = discord.Embed(title=title, color=discord.Color.from_rgb(139, 69, 19))
        embed.add_field(name="You received:", value=f"**{song}** - {artist}\n from *{album[1]}*")

        if guaranteed_sig:
            embed.add_field(name="🖋️ Remaining Signature Vinyl Pulls",
                            value=f"{get_sig_vinyl_count(ctx.author.id)} pulls left", inline=True)
        else:
            embed.add_field(name="💽 Remaining Vinyl Pulls",
                            value=f"{get_vinyl_count(ctx.author.id)} pulls left", inline=True)

        embed.set_image(url=f"attachment://{media_name}")
        file = discord.File(media_buf, filename=media_name, spoiler=False)

        add_song_to_collection(ctx.author.id, song_id, album_id,
                               variant=("sig_vinyl" if is_sig else "vinyl"))
        await ctx.send(embed=embed, file=file)

    @commands.hybrid_command(aliases=['sv'], description="Pull a guaranteed signature vinyl song")
    async def sigvinyl(self, ctx):
        """Pull a guaranteed signature vinyl rarity song"""
        await self._do_vinyl_pull(ctx, guaranteed_sig=True)

    @commands.hybrid_command(aliases=['v'], description="Pull a vinyl song from your rewards")
    async def vinyl(self, ctx):
        """Pull a vinyl rarity song from rewards"""
        await self._do_vinyl_pull(ctx, guaranteed_sig=False)

    @vinyl.error
    async def vinyl_error(self, ctx, error):
        if isinstance(error, commands.CommandError):
            await ctx.send(
                "❌ **Error pulling vinyl song!** Please try again later.")

    @xp.error
    async def xp_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send("❌ **Invalid user!** Please mention a valid user.")

    @level.error
    async def level_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send("❌ **Invalid user!** Please mention a valid user.")

    @claimreward.error
    async def claimreward_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing level number!**\n"
                           "**Usage:** `.claimreward <level>`\n"
                           "**Example:** `.claimreward 5`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(
                "❌ **Invalid level number!** Please provide a valid number between 1-25."
            )

    @commands.command(aliases=['gv'])
    @commands.has_role("bot_admin")
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
            description=f"**{user.display_name}** received **{amount}** vinyl pull{'s' if amount > 1 else ''}!",
            color=discord.Color.green()
        )
        
        embed.add_field(
            name="💽 Total Vinyl Pulls",
            value=f"{total_vinyl_count} available",
            inline=True
        )
        
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="Use .vinyl to claim vinyl songs!")
        
        await ctx.send(embed=embed)
        
        # Try to DM the user about the gift (optional, won't fail if DMs are closed)
        try:
            dm_embed = discord.Embed(
                title="🎁 You received vinyl pulls!",
                description=f"A bot admin granted you **{amount}** vinyl pull{'s' if amount > 1 else ''}!",
                color=discord.Color.green()
            )
            dm_embed.add_field(
                name="💽 Total Available",
                value=f"{total_vinyl_count} vinyl pulls",
                inline=True
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

    @commands.command(aliases=['gsv'])
    @commands.has_role("bot_admin")
    async def give_sigvinyl(self, ctx, user: discord.User, amount: int = 1):
        """Give signature vinyl pulls to a user (bot_admin only)
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
        total_vinyl_count = get_sig_vinyl_count(user.id)

        # Create success embed
        embed = discord.Embed(
            title="🎁 Signature Vinyl Pulls Granted!",
            description=f"**{user.display_name}** received **{amount}** signature vinyl pull{'s' if amount > 1 else ''}!",
            color=discord.Color.green()
        )
        
        embed.add_field(
            name="🖋️ Total Signature Vinyl Pulls",
            value=f"{total_vinyl_count} available",
            inline=True
        )
        
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="Use .sigvinyl to claim signature vinyl songs!")

        await ctx.send(embed=embed)
        
        # Try to DM the user about the gift (optional, won't fail if DMs are closed)
        try:
            dm_embed = discord.Embed(
                title="🎁 You received signature vinyl pulls!",
                description=f"A bot admin granted you **{amount}** signature vinyl pull{'s' if amount > 1 else ''}!",
                color=discord.Color.green()
            )
            dm_embed.add_field(
                name="🖋️ Total Available",
                value=f"{total_vinyl_count} signature vinyl pulls",
                inline=True
            )
            dm_embed.set_footer(text="Use .sigvinyl in the server to claim your songs!")
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


async def setup(bot):
    await bot.add_cog(XPCog(bot))
