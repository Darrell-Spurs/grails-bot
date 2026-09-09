import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
from db import get_song_rarity_counts_by_artist, get_songs_by_artist_and_rarity, get_all_artists

# ✅ Interactive View for song list with pagination
class SongListView(discord.ui.View):
    def __init__(self, ctx: commands.Context, songs: list, artist_name: str, rarity: str, rarity_emoji: str, rarity_color, assign_cog=None):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.songs = songs
        self.artist_name = artist_name
        self.rarity = rarity
        self.rarity_emoji = rarity_emoji
        self.rarity_color = rarity_color
        self.assign_cog = assign_cog
        self.current_page = 0
        self.songs_per_page = 15
        self.total_pages = max(1, (len(songs) - 1) // self.songs_per_page + 1)
        
        # Update button states
        self.update_buttons()

    def update_buttons(self):
        """Update button states based on current page"""
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

    def get_current_page_embed(self):
        """Generate embed for current page"""
        start_idx = self.current_page * self.songs_per_page
        end_idx = min(start_idx + self.songs_per_page, len(self.songs))
        current_songs = self.songs[start_idx:end_idx]
        
        song_text = ""
        for i, song in enumerate(current_songs, start=start_idx + 1):
            song_id, song_name, _, _ = song
            song_text += f"`{i}.` {song_name}\n"
        
        embed = discord.Embed(
            title=f"{self.rarity_emoji} {self.rarity.title()} Songs by {self.artist_name}",
            description=f"Showing **{len(self.songs)}** {self.rarity.lower()} songs",
            color=self.rarity_color
        )
        
        embed.add_field(
            name=f"Page {self.current_page + 1} / {self.total_pages}",
            value=song_text if song_text else "No songs found",
            inline=False
        )
        
        footer_text = f"Use .rarity {self.artist_name} to see full rarity distribution"
        embed.set_footer(text=footer_text)
        return embed

    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.primary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # if interaction.user != self.ctx.author:
        #     await interaction.response.send_message("This isn't your song list!", ephemeral=True)
        #     return
        
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            embed = self.get_current_page_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="▶️ Next", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # if interaction.user != self.ctx.author:
        #     await interaction.response.send_message("This isn't your song list!", ephemeral=True)
        #     return
        
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_buttons()
            embed = self.get_current_page_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # if interaction.user != self.ctx.author:
        #     await interaction.response.send_message("This isn't your song list!", ephemeral=True)
        #     return
        
        embed = self.get_current_page_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="❌ Close", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # if interaction.user != self.ctx.author:
        #     await interaction.response.send_message("This isn't your song list!", ephemeral=True)
        #     return
        
        await interaction.response.edit_message(view=None)

    async def on_timeout(self):
        # Disable all buttons when the view times out
        for item in self.children:
            item.disabled = True

class RarityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(aliases=['r'])
    async def rarity(self, ctx, *, artist_name: str = None):
        """
        Show rarity distribution for an artist.
        Usage: .rarity <artist_name>
        """
        
        if not artist_name:
            await ctx.send("❌ **Missing artist name!**\n"
                          "**Usage:** `.rarity <artist_name>`\n"
                          "**Example:** `.rarity Taylor Swift`")
            return
        
        # Check if artist exists in database
        all_artists = get_all_artists()
        artist_found = None
        for artist in all_artists:
            if artist.lower() == artist_name.lower():
                artist_found = artist
                break
        
        if not artist_found:
            await ctx.send(f"❌ **Artist not found!**\n"
                          f"Could not find artist `{artist_name}` in the database.")
            return
        
        # Get rarity counts for the artist
        rarity_counts = get_song_rarity_counts_by_artist(artist_found)
        
        if not rarity_counts:
            await ctx.send(f"❌ **No songs found!**\n"
                          f"No songs found for `{artist_found}` in the database.")
            return
        
        # Calculate total songs
        total_songs = sum(count for _, count in rarity_counts)
        
        # Create embed
        embed = discord.Embed(
            title=f"🎵 Rarity Distribution for {artist_found}",
            description=f"Total songs: **{total_songs}**",
            color=discord.Color.blurple()
        )
        
        # Add rarity breakdown
        rarity_text = []
        for rarity, count in rarity_counts:
            percentage = (count / total_songs) * 100
            rarity_emoji = self._get_rarity_emoji(rarity)
            if rarity.title() == "Common":
                rarity_text.append(f"{rarity_emoji} **N/A**: {count} songs ({percentage:.1f}%)")
            else:
                rarity_text.append(f"{rarity_emoji} **{rarity.title()}**: {count} songs ({percentage:.1f}%)")
        
        embed.add_field(
            name="Rarity Breakdown",
            value="\n".join(rarity_text),
            inline=False
        )
        
        embed.set_footer(text="Use .showrarity <artist> <rarity> to see songs of a specific rarity")
        await ctx.send(embed=embed)

    @commands.command(aliases=['sr'])
    async def showrarity(self, ctx, *, args: str = None):
        """
        Show all songs of a specific rarity for an artist.
        Usage: .showrarity <artist_name> <rarity>
        Valid rarities: ultimate, legendary, elite, unique, basic
        """
        
        if not args:
            await ctx.send("❌ **Missing arguments!**\n"
                          "**Usage:** `.showrarity <artist_name> <rarity>`\n"
                          "**Valid rarities:** `ultimate`, `legendary`, `elite`, `unique`, `basic`\n"
                          "**Example:** `.showrarity Taylor Swift legendary`")
            return
        
        # Parse arguments - last word is rarity, everything else is artist name
        args_parts = args.strip().split()
        if len(args_parts) < 2:
            await ctx.send("❌ **Missing arguments!**\n"
                          "**Usage:** `.showrarity <artist_name> <rarity>`\n"
                          "**Valid rarities:** `ultimate`, `legendary`, `elite`, `unique`, `basic`\n"
                          "**Example:** `.showrarity Taylor Swift legendary`")
            return
        
        rarity = args_parts[-1]  # Last word is rarity
        artist_name = " ".join(args_parts[:-1])  # Everything else is artist name
        
        # Validate rarity
        valid_rarities = ["ultimate", "legendary", "elite", "unique", "basic", "ul", "l", "e", "u", "b", "common"]
        if rarity.lower() not in valid_rarities:
            await ctx.send(f"❌ **Invalid rarity!**\n"
                          f"Valid rarities: {', '.join([f'`{r}`' for r in valid_rarities[:4]])}")
            return
        
        if len(rarity) <= 2:
            rarity = {
                "ul": "ultimate",
                "l": "legendary",
                "e": "elite",
                "u": "unique",
                "b": "basic"
            }.get(rarity.lower(), rarity)
        
        # Check if artist exists in database
        all_artists = get_all_artists()
        artist_found = None
        for artist in all_artists:
            if artist.lower() == artist_name.lower():
                artist_found = artist
                break
        
        if not artist_found:
            await ctx.send(f"❌ **Artist not found!**\n"
                          f"Could not find artist `{artist_name}` in the database.")
            return
        
        # Get songs of the specified rarity for the artist
        songs = get_songs_by_artist_and_rarity(artist_found, rarity.lower())
        
        if not songs:
            await ctx.send(f"❌ **No {rarity.lower()} songs found!**\n"
                          f"No {rarity.lower()} songs found for `{artist_found}`.")
            return
        
        # Remove duplicates while preserving order
        unique_songs = []
        seen_ids = set()
        for song in songs:
            song_id = song[0]
            if song_id not in seen_ids:
                unique_songs.append(song)
                seen_ids.add(song_id)
        
        # Create interactive view with pagination
        rarity_emoji = self._get_rarity_emoji(rarity.lower())
        rarity_color = self._get_rarity_color(rarity.lower())
        
        # Get reference to assign cog and automatically set as active list
        assign_cog = self.bot.get_cog('AssignCog')
        if assign_cog:
            # Convert songs to assign format and set as active list automatically
            assign_list = []
            for song in unique_songs:
                song_id, song_name, album_name, _ = song
                assign_list.append({
                    'id': song_id,
                    'name': song_name,
                    'artist': artist_found,
                    'album': album_name
                })
            assign_cog.set_active_list(ctx.channel.id, assign_list)
        
        view = SongListView(ctx, unique_songs, artist_found, rarity.lower(), rarity_emoji, rarity_color, assign_cog)
        embed = view.get_current_page_embed()
        
        await ctx.send(embed=embed, view=view)

    def _get_rarity_emoji(self, rarity):
        """Get emoji for each rarity"""
        emojis = {
            "ultimate": "🪐",
            "legendary": "🏆",
            "elite": "⭐",
            "unique": "🔮",
            "basic": "📄",
            "common": "❓"
        }
        return emojis.get(rarity, "🎵")

    def _get_rarity_color(self, rarity):
        """Get Discord color for each rarity"""
        colors = {
            "ultimate": discord.Color.gold(),
            "legendary": discord.Color.orange(),
            "elite": discord.Color.red(),
            "unique": discord.Color.purple(),
            "basic": discord.Color.blue(),
        }
        return colors.get(rarity, discord.Color.blue())

    @rarity.error
    async def rarity_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'bot_admin' role to use this command!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing artist name!**\n"
                          "**Usage:** `.rarity <artist_name>`\n"
                          "**Example:** `.rarity Taylor Swift`")
        else:
            raise error

    @showrarity.error
    async def showrarity_error(self, ctx, error):
        if isinstance(error, commands.MissingRole):
            await ctx.send("❌ You need the 'bot_admin' role to use this command!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing arguments!**\n"
                          "**Usage:** `.showrarity <artist_name> <rarity>`\n"
                          "**Valid rarities:** `ultimate`, `legendary`, `elite`, `unique`, `basic`\n"
                          "**Example:** `.showrarity Taylor Swift legendary`")
        else:
            raise error

async def setup(bot):
    await bot.add_cog(RarityCog(bot))
