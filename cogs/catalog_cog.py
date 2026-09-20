"""Browsing the pull pool: which artists exist, what they released, what can drop.

Replaces the old one-command cogs (.artists / .album / .single / .song). All
three are slash-only and read the catalog, not Spotify: a player asking "what
albums does this artist have" means "what can I actually pull", and the Spotify
listing includes releases that were never imported.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from utils import aesthetics
from utils.collection_ui import RARITY_DOT, RARITY_ORDER, UNASSIGNED, rarity_rank
from utils.command_types import slash_only
from utils.errors import report_unhandled

log = logging.getLogger("grails.catalog")

PER_PAGE = 15


async def _catalog_artist_autocomplete(interaction: discord.Interaction, current: str):
    """Every artist in the pool, unlike the collection autocompletes which are
    scoped to what one player owns."""
    cur = (current or "").lower()
    try:
        artists = db.get_all_artists()
    except Exception:
        log.exception("catalog artist autocomplete failed")
        return []
    return [app_commands.Choice(name=a, value=a) for a in artists if cur in a.lower()][:25]


async def _rarity_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    return [app_commands.Choice(name=r.title(), value=r)
            for r in RARITY_ORDER if cur in r][:25]


def _resolve_artist(name):
    """Match an artist case-insensitively, so typed input works as well as a
    picked autocomplete value."""
    if not name:
        return None
    lowered = name.strip().lower()
    for a in db.get_all_artists():
        if a.lower() == lowered:
            return a
    for a in db.get_all_artists():
        if lowered in a.lower():
            return a
    return None


class Paginator(discord.ui.View):
    """Prev/next over a list of preformatted lines."""

    def __init__(self, invoker_id, lines, title, colour, footer_note=""):
        super().__init__(timeout=180)
        self.invoker_id = int(invoker_id)
        self.lines = lines
        self.title = title
        self.colour = colour
        self.footer_note = footer_note
        self.page = 0
        self.message = None
        if self.pages <= 1:
            self.clear_items()

    @property
    def pages(self):
        return max((len(self.lines) + PER_PAGE - 1) // PER_PAGE, 1)

    def render(self):
        chunk = self.lines[self.page * PER_PAGE:(self.page + 1) * PER_PAGE]
        embed = discord.Embed(title=self.title, description="\n".join(chunk) or "_Nothing here._",
                              colour=self.colour)
        bits = [f"Page {self.page + 1}/{self.pages}", f"{len(self.lines)} total"]
        if self.footer_note:
            bits.append(self.footer_note)
        embed.set_footer(text="  ·  ".join(bits))
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.label == "Prev":
                    child.disabled = self.page <= 0
                elif child.label == "Next":
                    child.disabled = self.page >= self.pages - 1
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "That menu belongs to someone else — run the command yourself.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, button):
        self.page = max(self.page - 1, 0)
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page = min(self.page + 1, self.pages - 1)
        await interaction.response.edit_message(embed=self.render(), view=self)


ARTIST_SORTS = {
    "collected": "Most collected",
    "alpha": "A–Z",
    "favorited": "Most favorited",
}


class ArtistsView(Paginator):
    """The artist list, re-rankable without another database round trip.

    All three orderings are computed from one snapshot taken when the command
    runs: the counts are two small GROUP BYs, and fetching them once is cheaper
    than a query per press of the menu.
    """

    def __init__(self, invoker_id, artists, collected, favorited):
        self.artists = artists
        self.collected = collected
        self.favorited = favorited
        self.sort = "collected"
        super().__init__(invoker_id, self._lines(), "Artists Available",
                         discord.Colour(0x5B4BDB))
        # Paginator drops its children when there is only one page; the sort
        # menu has to survive that, since re-ranking is useful either way.
        self._install_select()

    def _lines(self):
        if self.sort == "alpha":
            ranked = sorted(self.artists, key=str.lower)
            return [f"`{i:>2}` **{a}**" for i, a in enumerate(ranked, 1)]

        counts = self.collected if self.sort == "collected" else self.favorited
        # Ties break alphabetically so the order is stable between renders.
        ranked = sorted(self.artists, key=lambda a: (-counts.get(a.lower(), 0), a.lower()))

        lines = []
        for i, artist in enumerate(ranked, 1):
            n = counts.get(artist.lower(), 0)
            if self.sort == "collected":
                tail = f"{n} cop{'y' if n == 1 else 'ies'}"
            else:
                tail = f"{n} {'person' if n == 1 else 'people'}"
            lines.append(f"`{i:>2}` **{artist}**  ·  {tail}")
        return lines

    def _install_select(self):
        select = discord.ui.Select(
            placeholder=f"Sort: {ARTIST_SORTS[self.sort]}",
            row=1,
            options=[discord.SelectOption(label=label, value=key,
                                          default=(key == self.sort))
                     for key, label in ARTIST_SORTS.items()],
        )
        select.callback = self._on_sort
        self._sort_select = select
        self.add_item(select)

    async def _on_sort(self, interaction):
        self.sort = self._sort_select.values[0]
        self.page = 0
        self.lines = self._lines()
        self.clear_items()
        # Rebuilding puts the paging buttons back in the right disabled state
        # for a list whose length has not changed but whose page has reset.
        if self.pages > 1:
            self.add_item(self.prev)
            self.add_item(self.next)
        self._install_select()
        await interaction.response.edit_message(embed=self.render(), view=self)



class CatalogCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---- /artists ---------------------------------------------------------
    @commands.hybrid_command(name="artists", description="List every artist in the pull pool")
    @slash_only()
    async def artists(self, ctx):
        await ctx.defer()
        overview, collected, favorited = await asyncio.gather(
            asyncio.to_thread(db.get_artist_overview),
            asyncio.to_thread(db.get_artist_collected_counts),
            asyncio.to_thread(db.get_artist_favourite_counts),
        )
        if not overview:
            await ctx.send("📭 No artists in the pool yet.")
            return

        view = ArtistsView(ctx.author.id, [a["artist"] for a in overview],
                           collected, favorited)
        # Always attach the view: even on a single page the sort menu is live.
        view.message = await ctx.send(embed=view.render(), view=view)

    # ---- /albums ----------------------------------------------------------
    @commands.hybrid_command(name="albums", description="List an artist's releases in the pool")
    @slash_only()
    @app_commands.describe(artist="Which artist")
    @app_commands.autocomplete(artist=_catalog_artist_autocomplete)
    async def albums(self, ctx, *, artist: str):
        await ctx.defer()
        resolved = await self.bot.loop.run_in_executor(None, _resolve_artist, artist)
        if not resolved:
            await ctx.send(f"🔍 **{artist}** is not in the pool. Try `/artists` to see who is.")
            return

        songs = await self.bot.loop.run_in_executor(
            None, db.get_songs_with_ids_by_artist, resolved)
        if not songs:
            await ctx.send(f"📭 No releases linked to **{resolved}** yet.")
            return

        # (song_id, name, rarity, album_name, track_count, image, album_id)
        albums = {}
        for _sid, _name, _rarity, album_name, track_count, _img, album_id in songs:
            if album_id not in albums:
                albums[album_id] = {"name": album_name,
                                    "category": db.get_album_category(track_count),
                                    "tracks": 0}
            albums[album_id]["tracks"] += 1

        order = {"Album": 0, "EP": 1, "Single": 2}
        rows = sorted(albums.values(),
                      key=lambda a: (order.get(a["category"], 3), a["name"].lower()))
        lines = [f"**{a['name']}** — {a['category']} · {a['tracks']} track"
                 f"{'' if a['tracks'] == 1 else 's'}" for a in rows]

        view = Paginator(ctx.author.id, lines, f"💿 {resolved} — releases",
                         discord.Colour(0x5B4BDB))
        view.message = await ctx.send(embed=view.render(), view=view if view.pages > 1 else None)

    # ---- /songs -----------------------------------------------------------
    @commands.hybrid_command(name="songs", description="List an artist's songs, with an optional rarity filter")
    @slash_only()
    @app_commands.describe(artist="Which artist", rarity="Only show this rarity")
    @app_commands.autocomplete(artist=_catalog_artist_autocomplete, rarity=_rarity_autocomplete)
    async def songs(self, ctx, artist: str, rarity: Optional[str] = None):
        await ctx.defer()
        resolved = await self.bot.loop.run_in_executor(None, _resolve_artist, artist)
        if not resolved:
            await ctx.send(f"🔍 **{artist}** is not in the pool. Try `/artists` to see who is.")
            return

        wanted = (rarity or "").lower() or None
        if wanted and wanted not in RARITY_ORDER:
            await ctx.send(f"❌ **{rarity}** is not a rarity. Pick one of: "
                           + ", ".join(f"`{r}`" for r in RARITY_ORDER[:5]))
            return

        rows = await self.bot.loop.run_in_executor(
            None, db.get_songs_with_ids_by_artist, resolved)
        if not rows:
            await ctx.send(f"📭 No songs linked to **{resolved}** yet.")
            return

        # A song on several albums appears once per album; collapse to one entry.
        songs = {}
        for song_id, name, song_rarity, album_name, _tc, _img, _aid in rows:
            songs.setdefault(song_id, {"name": name, "rarity": (song_rarity or UNASSIGNED).lower(),
                                       "album": album_name})
        entries = list(songs.values())

        counts = {}
        for e in entries:
            counts[e["rarity"]] = counts.get(e["rarity"], 0) + 1

        if wanted:
            entries = [e for e in entries if e["rarity"] == wanted]
            if not entries:
                have = ", ".join(f"{RARITY_DOT.get(r, '')} {r} ({counts[r]})"
                                 for r in RARITY_ORDER if counts.get(r))
                await ctx.send(f"📭 **{resolved}** has no **{wanted}** songs.\nAvailable: {have}")
                return

        entries.sort(key=lambda e: (rarity_rank(e["rarity"]), e["name"].lower()))
        lines = [f"{RARITY_DOT.get(e['rarity'], '')} **{e['name']}** — _{e['album'] or '—'}_"
                 for e in entries]

        title = f"🎵 {resolved} — songs"
        if wanted:
            title += f" · {wanted}"
        spread = "  ".join(f"{RARITY_DOT[r]}{counts[r]}" for r in RARITY_ORDER if counts.get(r))
        top = min((rarity_rank(e["rarity"]) for e in entries), default=len(RARITY_ORDER))
        colour = discord.Colour(
            aesthetics.rarity_colour_int(RARITY_ORDER[top]) if top < len(RARITY_ORDER)
            else aesthetics.hex_to_int(aesthetics.ACCENT_COLOR))

        view = Paginator(ctx.author.id, lines, title, colour, footer_note=spread)
        view.message = await ctx.send(embed=view.render(), view=view if view.pages > 1 else None)

    # ---- errors -----------------------------------------------------------
    @artists.error
    async def artists_error(self, ctx, error):
        await report_unhandled(log, ctx, error, command="artists")

    @albums.error
    async def albums_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Name an artist — try `/albums artist:<name>`.")
            return
        await report_unhandled(log, ctx, error, command="albums")

    @songs.error
    async def songs_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Name an artist — try `/songs artist:<name>`.")
            return
        await report_unhandled(log, ctx, error, command="songs")


async def setup(bot):
    await bot.add_cog(CatalogCog(bot))
