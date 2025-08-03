import os, sys

from db import add_song_to_collection
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discord
from discord.ext import commands
from utils.helpers import get_random_song, make_3_song_collage, get_random_album, draw_variant
from utils.mystic import main as generate_mythic_gif
 
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
        add_song_to_collection(interaction.user.id, song_id=song_id, album_id=album_id, variant=variant)
        print(f"Added {variant} {song_name} by {artist} to {interaction.user.display_name}'s collection")

        variant_text = f" *({variant})*" if variant != "default" else ""
        await interaction.message.channel.send(f"**{interaction.user.display_name}** picked: **{song_name}**{variant_text} by **{artist}**")
        await interaction.message.edit(view=None)  # Optional: remove buttons after choice

# ✅ View for mythic claim button
class MythicClaimView(discord.ui.View):
    def __init__(self, ctx: commands.Context, song_id: int, album_id: int, song_name: str, artist: str):
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

        # Add to collection
        add_song_to_collection(interaction.user.id, song_id=self.song_id, album_id=self.album_id, variant="mythic")
        
        # Mark as claimed and disable button
        self.claimed = True
        button.disabled = True
        button.label = "✅ Claimed!"
        button.style = discord.ButtonStyle.secondary
        
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"**{interaction.user.display_name}** claimed the mythic: **{self.song_name}** by **{self.artist}**! 💎")

    async def on_timeout(self):
        # Disable button when view times out
        for item in self.children:
            item.disabled = True
        # Note: The message won't be updated automatically on timeout unless we have a reference to it

# ✅ Cog containing the command
class ChoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.cooldown(rate=1, per=5, type=commands.BucketType.user)
    async def choice(self, ctx):
        # Fetch 3 random songs
        song1, id_1, artist1 = get_random_song()
        song2, id_2, artist2 = get_random_song()
        song3, id_3, artist3 = get_random_song()

        album1 = get_random_album(id_1)
        album2 = get_random_album(id_2)
        album3 = get_random_album(id_3)

        songs = [song1, song2, song3]
        image_urls = [album1[2], album2[2], album3[2]]
        ids = [id_1, id_2, id_3]
        titles = [song1, song2, song3]
        artists = [artist1, artist2, artist3]
        albums = [album1, album2, album3]

        collage_path = "choices.jpg"
        variants = [draw_variant(), draw_variant(), draw_variant()]

        if "mythic" in variants:

            index = variants.index("mythic")
            song_name = songs[index]
            song_id = ids[index]
            album = albums[index]
            album_id = album[0]
            album_name = album[1]
            album_url = album[2]
            artist = artists[index]
            print(song_name, album_name, artist)

            embed = discord.Embed(
                title=f"💎 **{ctx.author.display_name} PULLED A MYTHIC SONG** 💎",
                description=f"||**{song_name}**|| by ||**{artist}**|| from ||*{album_name}*||\n\nClick the button below to claim this mythic song!",
                color=discord.Color.from_rgb(140, 202, 247)
            )

            generate_mythic_gif(album_url)
            gif_path = "mythic_animated.gif"
            
            # Create the claim view
            claim_view = MythicClaimView(ctx, song_id, album_id, song_name, artist)
            
            if os.path.exists(gif_path):
                # Use the filename without SPOILER_ prefix when spoiler=True
                file = discord.File(gif_path, filename="mythic_animated.gif", spoiler=True)
                embed.set_image(url="attachment://mythic_animated.gif")
                await ctx.send(embed=embed, file=file, view=claim_view)
            else:
                await ctx.send(embed=embed, content="(Missing mythic gif ❗)", view=claim_view)

            return  
        
        make_3_song_collage(image_urls, titles, artists, variants=variants, output_path=collage_path)
        file = discord.File(collage_path, filename="choices.jpg")

        embed = discord.Embed(
            title="🎶 **Make a choice**🎶",
            description="Click a button to pick your preferred song!",
            color=discord.Color.blurple()
        )
        embed.set_image(url="attachment://choices.jpg")

        await ctx.send(embed=embed, file=file, view=ChooseSongView(ctx, options=[(ids[i], titles[i], artists[i], albums[i], variants[i]) for i in range(3)]))

    @choice.error
    async def choice_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Cool down! Try again in {round(error.retry_after)}s.")
            return

async def setup(bot):
    await bot.add_cog(ChoiceCog(bot))
