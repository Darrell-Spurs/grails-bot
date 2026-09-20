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
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from utils import aesthetics
from utils.collection_ui import (RARITY_ORDER, UNASSIGNED, rarity_emoji, rarity_rank,
                                 safe_option_emoji)
from utils.command_types import slash_only
from utils.helpers import cached_artists
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


# Album lists are read once per keystroke while someone types into the album
# argument, and the query costs ~185ms against a remote database. A short TTL
# keeps a burst of typing to one round trip without letting the list go stale
# for longer than it takes to notice a newly imported release.
_ALBUM_TTL = 60.0
_album_cache = {}


def _cached_album_counts(artist):
    key = artist.lower()
    now = time.time()
    hit = _album_cache.get(key)
    if hit and now < hit[0]:
        return hit[1]
    rows = db.get_album_song_counts(artist)
    _album_cache[key] = (now + _ALBUM_TTL, rows)
    return rows


async def _album_autocomplete(interaction: discord.Interaction, current: str):
    """Albums for whichever artist has already been chosen.

    Discord shows 25 suggestions but filters against the whole list as the user
    types, so every release stays reachable -- which the select menu cannot
    manage, since it is hard-capped at 25 options and Taylor Swift alone has 71
    albums.
    """
    artist = getattr(interaction.namespace, "artist", None)
    if not artist:
        return [app_commands.Choice(name="Pick an artist first", value="")]

    resolved = _resolve_artist(artist)
    if not resolved:
        return []
    try:
        rows = _cached_album_counts(resolved)
    except Exception:
        log.exception("album autocomplete failed for %s", resolved)
        return []

    cur = (current or "").lower()
    choices = []
    for name, count in rows:               # already ordered fullest-first
        if cur and cur not in name.lower():
            continue
        # Discord caps a choice name at 100 characters and several soundtrack
        # releases genuinely run longer.
        label = f"{name} ({count})"
        choices.append(app_commands.Choice(name=label[:100], value=name[:100]))
        if len(choices) == 25:
            break
    return choices or [app_commands.Choice(name="No albums match", value="")]


def _resolve_album(artist, name):
    """Match a typed album against one of `artist`'s releases, or None.

    Autocomplete sends the exact name, but the value is truncated at 100
    characters and a player may type freehand, so exact, prefix and substring
    matches are all accepted.
    """
    if not name:
        return None
    wanted = name.strip().lower()
    albums = [a for a, _ in _cached_album_counts(artist)]
    for a in albums:
        if a.lower() == wanted:
            return a
    for a in albums:                       # autocomplete truncates at 100 chars
        if a.lower().startswith(wanted):
            return a
    for a in albums:
        if wanted in a.lower():
            return a
    return None


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
    # Cached, not a fresh query: autocomplete resolves the artist on every
    # keystroke, and this used to read the whole artist table twice per call.
    artists = cached_artists()
    for a in artists:
        if a.lower() == lowered:
            return a
    for a in artists:
        if lowered in a.lower():
            return a
    return None


class Paginator(discord.ui.View):
    """Prev/next over a list of preformatted lines.

    Anyone may drive it. Every subclass browses the public catalogue, where the
    controls only choose which slice is on screen -- refusing a bystander's
    click buys nothing and just makes them re-run the command to read the same
    list. Views that act on one person's data lock themselves instead
    (CardView, ProfileView in utils/collection_ui.py and cogs/profile_cog.py).
    """

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
                tail = f"{n} claimed songs"
            else:
                tail = f"{n} favorited"
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



# Release categories, longest first. The emoji replaces the word on every line,
# so the three have to be tellable apart at Discord's ~18px, not merely be from
# the same family -- that is why the Single is a note rather than a third disc.
ALBUM_CATEGORIES = ("Album", "EP", "Single")
ALBUM_FILTERS = {"Album": "Albums", "EP": "EPs", "Single": "Singles"}
ALBUM_CATEGORY_EMOJI = {"Album": "\U0001F4BF", "EP": "\U0001F4BD", "Single": "\U0001F3B5"}


class AlbumsView(Paginator):
    """An artist's releases, one category at a time.

    Sorted A-Z. Release date would order these better, but the albums table
    holds only id, name, artist, artist_id and image -- no date is imported, so
    there is nothing to sort on.

    Filtering works over a snapshot taken when the command ran: one artist's
    release list is small, so re-filtering in memory beats a round trip per
    press of the menu.
    """

    def __init__(self, invoker_id, artist, releases):
        self.artist = artist
        self.releases = releases
        self.counts = {c: sum(1 for r in releases if r["category"] == c)
                       for c in ALBUM_CATEGORIES}
        # Open on the fullest release type the artist actually has: a singles
        # artist should not land on an empty "Albums" page.
        self.category = next((c for c in ALBUM_CATEGORIES if self.counts[c]),
                             ALBUM_CATEGORIES[0])
        super().__init__(invoker_id, self._lines(), self._title(),
                         discord.Colour(0x5B4BDB))
        # Paginator clears its children on a single page; the filter has to
        # outlive that, since switching category is the point of the menu.
        self._install_select()

    def _title(self):
        return f"{self.artist} — {ALBUM_FILTERS[self.category]}"

    def _lines(self):
        rows = sorted((r for r in self.releases if r["category"] == self.category),
                      key=lambda a: a["name"].lower())
        emoji = ALBUM_CATEGORY_EMOJI[self.category]
        return [f"{emoji} **{discord.utils.escape_markdown(a['name'])}**  ·  {a['tracks']} track"
                f"{'' if a['tracks'] == 1 else 's'}" for a in rows]

    def render(self):
        self.title = self._title()
        embed = super().render()
        if not self.counts[self.category]:
            embed.description = (f"_No {ALBUM_FILTERS[self.category].lower()} "
                                 f"for **{self.artist}**._")
        return embed

    def _install_select(self):
        select = discord.ui.Select(
            placeholder="Show…", row=1,
            options=[discord.SelectOption(label=f"{ALBUM_FILTERS[c]} ({self.counts[c]})",
                                          value=c,
                                          emoji=safe_option_emoji(ALBUM_CATEGORY_EMOJI[c]),
                                          default=(c == self.category))
                     for c in ALBUM_CATEGORIES],
        )
        select.callback = self._on_filter
        self._filter_select = select
        self.add_item(select)

    async def _on_filter(self, interaction):
        self.category = self._filter_select.values[0]
        self.page = 0
        self.lines = self._lines()
        self.clear_items()
        if self.pages > 1:
            self.add_item(self.prev)
            self.add_item(self.next)
        self._install_select()
        await interaction.response.edit_message(embed=self.render(), view=self)



# A select may hold 25 options, one of which is "All albums".
MAX_ALBUM_OPTIONS = 24


class SongsView(Paginator):
    """An artist's songs, filterable by album.

    Entries carry the *set* of albums a song appears on, not just one: a track
    that is on both the standard and the deluxe edition has to be findable under
    either, and collapsing it to a single album would hide it from one of them.
    """

    def __init__(self, invoker_id, artist, entries, counts, rarity=None, album=None):
        self.artist = artist
        self.entries = entries
        self.counts = counts
        self.rarity = rarity
        self.album = album or "all"

        seen = {}
        for e in entries:
            for name in e["albums"]:
                seen[name] = seen.get(name, 0) + 1
        # Ordered by how many songs each release contributes, not A-Z: albums
        # and EPs then sort to the top and the one-track singles fall to the
        # bottom, which is both the order people look in and -- since the menu
        # can only hold 24 -- means anything cut is a single rather than a
        # record.
        self.albums = sorted(seen, key=lambda name: (-seen[name], name.lower()))
        self.album_counts = seen

        super().__init__(invoker_id, self._lines(), self._title(),
                         self._colour(), footer_note=self._spread())
        self._install_select()

    # -- data ---------------------------------------------------------------
    def _visible(self):
        if self.album == "all":
            return self.entries
        return [e for e in self.entries if self.album in e["albums"]]

    def _lines(self):
        rows = sorted(self._visible(),
                      key=lambda e: (rarity_rank(e["rarity"]), e["name"].lower()))
        lines = []
        for e in rows:
            # Under a filter the album is already in the title, so the tail
            # would just repeat it on every row.
            if self.album == "all":
                album = sorted(e["albums"], key=str.lower)[0] if e["albums"] else "—"
                lines.append(f"{rarity_emoji(e['rarity'])} **{e['name']}** — _{album}_")
            else:
                lines.append(f"{rarity_emoji(e['rarity'])} **{e['name']}**")
        return lines

    def _title(self):
        if self.album == "all":
            title = f"{self.artist} — Discography"
        else:
            title = f"{self.artist} — songs from {self.album}"
        if self.rarity:
            title += f" · {self.rarity}"
        return title[:256]

    def _spread(self):
        counts = {}
        for e in self._visible():
            counts[e["rarity"]] = counts.get(e["rarity"], 0) + 1
        spread = " ".join(f"{counts[r]} {r} /"
                           for r in RARITY_ORDER if counts.get(r))
        spread = spread.rstrip(" / ")
        return spread

    def _colour(self):
        top = min((rarity_rank(e["rarity"]) for e in self._visible()),
                  default=len(RARITY_ORDER))
        return discord.Colour(
            aesthetics.rarity_colour_int(RARITY_ORDER[top]) if top < len(RARITY_ORDER)
            else aesthetics.ACCENT_COLOR_INT)

    def render(self):
        self.title = self._title()
        self.colour = self._colour()
        self.footer_note = self._spread()
        embed = super().render()
        if not self._visible():
            embed.description = f"_Nothing here for **{self.album}**._"
        return embed

    # -- the album menu -----------------------------------------------------
    def _install_select(self):
        shown = self.albums[:MAX_ALBUM_OPTIONS]
        # The menu holds 24 releases; an album picked through the command
        # argument may not be among them, and the menu has to be able to show
        # what is currently selected. Pin it and drop the smallest instead.
        if self.album != "all" and self.album not in shown:
            shown = [self.album] + shown[:MAX_ALBUM_OPTIONS - 1]

        options = [discord.SelectOption(label=f"Full discography ({len(self.entries)})",
                                        value="all", default=(self.album == "all"))]
        for name in shown:
            options.append(discord.SelectOption(
                # Discord caps a label at 100 characters and some release names
                # genuinely run longer than that.
                label=f"{name} ({self.album_counts[name]})"[:100],
                value=name[:100],
                default=(name == self.album)))

        select = discord.ui.Select(placeholder="Filter by album…", row=1, options=options)
        select.callback = self._on_filter
        self._album_select = select
        self.add_item(select)

    async def _on_filter(self, interaction):
        self.album = self._album_select.values[0]
        self.page = 0
        self.lines = self._lines()
        self.clear_items()
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

        view = AlbumsView(ctx.author.id, resolved, list(albums.values()))
        # Always attach: the category filter is live even on a single page.
        view.message = await ctx.send(embed=view.render(), view=view)

    # ---- /songs -----------------------------------------------------------
    @commands.hybrid_command(name="songs", description="List an artist's songs, with an optional rarity filter")
    @slash_only()
    @app_commands.describe(artist="Which artist", rarity="Only show this rarity",
                           album="Only show songs from this release")
    @app_commands.autocomplete(artist=_catalog_artist_autocomplete,
                               rarity=_rarity_autocomplete,
                               album=_album_autocomplete)
    async def songs(self, ctx, artist: str, rarity: Optional[str] = None,
                    album: Optional[str] = None):
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

        # A song on several albums appears once per album. Collapse to one entry
        # but keep every album it belongs to, so the album filter can find it
        # under the deluxe edition as well as the standard one.
        songs = {}
        for song_id, name, song_rarity, album_name, _tc, _img, _aid in rows:
            entry = songs.setdefault(song_id, {
                "name": name,
                "rarity": (song_rarity or UNASSIGNED).lower(),
                "albums": set(),
            })
            if album_name:
                entry["albums"].add(album_name)
        entries = list(songs.values())

        counts = {}
        for e in entries:
            counts[e["rarity"]] = counts.get(e["rarity"], 0) + 1

        if wanted:
            entries = [e for e in entries if e["rarity"] == wanted]
            if not entries:
                have = ", ".join(f"{rarity_emoji(r)} {r} ({counts[r]})"
                                 for r in RARITY_ORDER if counts.get(r))
                await ctx.send(f"📭 **{resolved}** has no **{wanted}** songs.\nAvailable: {have}")
                return

        chosen = None
        if album:
            chosen = await asyncio.to_thread(_resolve_album, resolved, album)
            if not chosen:
                await ctx.send(f"🔍 **{resolved}** has no release called **{album}**. "
                               f"Leave it blank to see the whole discography.")
                return

        view = SongsView(ctx.author.id, resolved, entries, counts,
                         rarity=wanted, album=chosen)
        # Always attach: the album filter is live even on a single page.
        view.message = await ctx.send(embed=view.render(), view=view)

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
