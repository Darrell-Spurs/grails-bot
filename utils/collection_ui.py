"""Shared presentation for cards, collections and profiles.

One module rather than three so the two directions of the flow can reference
each other: a collection opens a card, and that card can go back to exactly the
collection it came from. Both live here; the cogs stay thin.

Design rules this encodes:
  * Rarity is never a filter -- it is the sort order. Variant is the filter.
  * Drilling into a card edits the existing message; it never posts a new one,
    so a channel keeps one collection message per invocation.
  * Only the person who ran the command may drive the components.
"""
import logging

import discord

import db
from utils import aesthetics

log = logging.getLogger("grails.ui")

# ---------------------------------------------------------------------------
# Vocabulary lives in utils/aesthetics.py. Re-exported here so the existing
# `from utils.collection_ui import RARITY_DOT, ...` call sites keep working.
# ---------------------------------------------------------------------------

from utils.aesthetics import (  # noqa: F401
    ACCENT_COLOR, RARITY_COLOR, RARITY_DOT, RARITY_ORDER, UNASSIGNED,
    TEXT_PRESENTATION_BASES, VARIANT_COLOR, VARIANT_LABEL, VARIANTS,
    card_emoji, rarity_rank, safe_option_emoji, variant_emoji, variant_rank,
)

SORTS = {
    "variant": "Variant (rarest within each)",
    "newest": "Newest first",
    "artist": "Artist A-Z",
    "song": "Song A-Z",
}

PER_PAGE = 10
TIMEOUT = 180


def rarity_colour(rarity):
    """discord.Colour for a rarity, from the shared palette."""
    return discord.Colour(aesthetics.rarity_colour_int(rarity))


def _dot(rarity):
    return aesthetics.rarity_dot(rarity)


# ---------------------------------------------------------------------------
# Card embed
# ---------------------------------------------------------------------------

def build_card_embed(card, owner_name, copy_number=None, owners=None, pinned=False):
    """One collected card. Album art comes straight from the stored Spotify URL,
    so nothing is rendered and the embed is instant."""
    rarity = (card["rarity"] or UNASSIGNED).lower()
    variant = (card["variant"] or "default").lower()

    title = f"{card_emoji(rarity, variant)} {card['song_name']}"
    if variant == "mythic" and copy_number:
        title += f"  ·  #{copy_number}"

    embed = discord.Embed(title=title, colour=rarity_colour(rarity))
    embed.set_author(name=f"{owner_name}'s card")
    embed.add_field(name="Artist", value=card["artist"] or "—", inline=True)
    embed.add_field(name="Album", value=card["album_name"] or "—", inline=True)

    if owners is not None:
        embed.add_field(
            name="In circulation",
            value=f"{owners} {'copy' if owners == 1 else 'copies'} held",
            inline=True,
        )

    if card.get("album_image"):
        embed.set_image(url=card["album_image"])

    footer = f"Card #{card['collection_id']}"
    if pinned:
        footer += "  ·  \U0001F4CC pinned to profile"
    if card.get("collected_at"):
        footer += f"  ·  pulled {_short_time(card['collected_at'])}"
    embed.set_footer(text=footer)
    return embed


def _short_time(value):
    text = str(value)
    return text[:16] if len(text) >= 16 else text


# ---------------------------------------------------------------------------
# Collection embed (design "Option A": text list, zero rendering)
# ---------------------------------------------------------------------------

def _card_line(index, card):
    """One row of the list.

    The leading glyph is the server emoji for this exact rarity+variant pair,
    so rarity and variant are already on the row -- spelling them out again in
    a second line would only halve how many cards fit on a page. The mythic
    copy number is the one thing the glyph cannot carry, so it rides inline.
    """
    rarity = (card["rarity"] or UNASSIGNED).lower()
    variant = (card["variant"] or "default").lower()

    copy_tag = ""
    if variant == "mythic" and card.get("copy_number"):
        copy_tag = f" #{card['copy_number']}"
    return (f"`{index:>2}` {card_emoji(rarity, variant)} "
            f"**{card['song_name']}** by **{card['artist']}**{copy_tag}")


def build_collection_embed(cards, page, pages, total, owner_name, *, variant=None,
                           artist=None, sort="variant"):
    lines = [_card_line(page * PER_PAGE + i + 1, c) for i, c in enumerate(cards)]

    title = f"\U0001F4D6 {owner_name}'s collection"
    if artist:
        title += f" · {artist}"

    # The colour bar takes the rarest tier on the page, so scrolling towards
    # the good stuff is visible before reading a word.
    top = min((rarity_rank(c["rarity"]) for c in cards), default=len(RARITY_ORDER))
    colour = rarity_colour(RARITY_ORDER[top]) if top < len(RARITY_ORDER) else discord.Colour(0x5B4BDB)

    embed = discord.Embed(
        title=title,
        description="\n".join(lines) if lines else "_Nothing here with those filters._",
        colour=colour,
    )

    bits = [f"Page {page + 1}/{max(pages, 1)}", f"{total} {'card' if total == 1 else 'cards'}"]
    if variant:
        bits.append(f"{VARIANT_LABEL.get(variant, variant)}")
    bits.append(SORTS.get(sort, sort).lower())
    embed.set_footer(text="  ·  ".join(bits))
    return embed


def sort_cards(cards, sort):
    if sort == "newest":
        return sorted(cards, key=lambda c: str(c.get("collected_at") or ""), reverse=True)
    if sort == "artist":
        return sorted(cards, key=lambda c: ((c["artist"] or "").lower(), (c["song_name"] or "").lower()))
    if sort == "song":
        return sorted(cards, key=lambda c: ((c["song_name"] or "").lower(), (c["artist"] or "").lower()))
    # Default: variant is the primary grouping -- mythics first, plain cards
    # last -- and rarity orders the songs *within* each variant. Artist/song
    # break remaining ties so the order is stable between renders.
    return sorted(cards, key=lambda c: (variant_rank(c["variant"]),
                                        rarity_rank(c["rarity"]),
                                        (c["artist"] or "").lower(),
                                        (c["song_name"] or "").lower()))


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class _OwnerView(discord.ui.View):
    """Base view that only lets the invoking user drive the components."""

    def __init__(self, invoker_id, timeout=TIMEOUT):
        super().__init__(timeout=timeout)
        self.invoker_id = int(invoker_id)
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "That menu belongs to someone else — run the command yourself to get your own.",
                ephemeral=True,
            )
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


class CardView(_OwnerView):
    """A single card, with the actions that make sense for who is looking."""

    def __init__(self, invoker_id, card, owner_name, *, origin=None, owner_id=None):
        super().__init__(invoker_id)
        self.card = card
        self.owner_name = owner_name
        self.owner_id = str(owner_id if owner_id is not None else card["user_id"])
        # `origin` is the collection state to restore; None when /view was
        # called directly, in which case there is nothing to go back to.
        self.origin = origin

        is_owner = str(invoker_id) == self.owner_id

        if origin is None:
            self.remove_item(self.back)
        if not is_owner:
            self.remove_item(self.trade_this)
            self.remove_item(self.pin_card)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction, button):
        view = self.origin
        view.invoker_id = self.invoker_id
        view.message = self.message
        await interaction.response.edit_message(embed=view.render(), view=view)

    @discord.ui.button(label="Trade this", style=discord.ButtonStyle.primary, row=0)
    async def trade_this(self, interaction, button):
        # The trade flow needs a counterparty and runs its own state machine, so
        # this hands over a ready-to-run command rather than forking it.
        variant = (self.card["variant"] or "default").lower()
        await interaction.response.send_message(
            f"To offer **{self.card['song_name']}**, run:\n"
            f"```/trade user:<who> rarity:{variant} item:{self.card['collection_id']}```"
            f"`item` is this card's id — pick it straight from autocomplete if you prefer.",
            ephemeral=True,
        )

    @discord.ui.button(label="Pin to profile", emoji="\U0001F4CC",
                       style=discord.ButtonStyle.secondary, row=0)
    async def pin_card(self, interaction, button):
        ok = db.set_pinned_card(self.invoker_id, self.card["collection_id"])
        if not ok:
            await interaction.response.send_message(
                "Could not pin that card — register with `.register` first.", ephemeral=True)
            return
        embed = build_card_embed(
            self.card, self.owner_name,
            copy_number=db.get_card_copy_number(self.card["collection_id"]),
            owners=db.count_card_owners(self.card["song_id"]),
            pinned=True,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"\U0001F4CC Pinned **{self.card['song_name']}** to your profile.", ephemeral=True)

    @discord.ui.button(label="View artist", style=discord.ButtonStyle.secondary, row=0)
    async def view_artist(self, interaction, button):
        # Shows the card owner's collection for that artist, not the viewer's --
        # otherwise clicking it on someone else's card would jump contexts.
        view = CollectionView(
            invoker_id=self.invoker_id,
            owner_id=self.owner_id,
            owner_name=self.owner_name,
            artist=self.card["artist"],
        )
        view.message = self.message
        await interaction.response.edit_message(embed=view.render(), view=view)


class CollectionView(_OwnerView):
    """Paginated collection list with variant/sort controls and a card opener."""

    def __init__(self, invoker_id, owner_id, owner_name, *, variant=None,
                 artist=None, sort="variant"):
        super().__init__(invoker_id)
        self.owner_id = str(owner_id)
        self.owner_name = owner_name
        self.variant = variant
        self.artist = artist
        self.sort = sort
        self.page = 0
        self.all_cards = []
        self.reload()

    # ---- data ----------------------------------------------------------
    def reload(self):
        cards = db.search_user_cards(self.owner_id, query="", variant=self.variant, limit=1000)
        if self.artist:
            wanted = self.artist.lower()
            cards = [c for c in cards if (c["artist"] or "").lower() == wanted]
        for card in cards:
            if (card["variant"] or "").lower() == "mythic":
                card["copy_number"] = db.get_card_copy_number(card["collection_id"])
        self.all_cards = sort_cards(cards, self.sort)
        self.page = min(self.page, max(self.pages - 1, 0))

    @property
    def total(self):
        return len(self.all_cards)

    @property
    def pages(self):
        return max((self.total + PER_PAGE - 1) // PER_PAGE, 1)

    @property
    def current(self):
        start = self.page * PER_PAGE
        return self.all_cards[start:start + PER_PAGE]

    # ---- rendering -----------------------------------------------------
    def render(self):
        self._sync_components()
        return build_collection_embed(
            self.current, self.page, self.pages, self.total, self.owner_name,
            variant=self.variant, artist=self.artist, sort=self.sort,
        )

    def _sync_components(self):
        self.prev_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.pages - 1

        opener = self.open_card
        opener.options = [
            discord.SelectOption(
                label=f"{c['song_name']} by {c['artist']}"[:100],
                description=f"{c['rarity']} · {VARIANT_LABEL.get((c['variant'] or 'default').lower(), c['variant'])}"[:100],
                emoji=safe_option_emoji(card_emoji(c["rarity"], c["variant"])),
                value=str(c["collection_id"]),
            )
            for c in self.current
        ] or [discord.SelectOption(label="Nothing to open", value="none")]
        opener.disabled = not self.current

        for option in self.variant_filter.options:
            option.default = (option.value == (self.variant or "all"))
        for option in self.sort_by.options:
            option.default = (option.value == self.sort)

    async def _refresh(self, interaction):
        await interaction.response.edit_message(embed=self.render(), view=self)

    # ---- components ----------------------------------------------------
    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_page(self, interaction, button):
        self.page = max(self.page - 1, 0)
        await self._refresh(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=0)
    async def next_page(self, interaction, button):
        self.page = min(self.page + 1, self.pages - 1)
        await self._refresh(interaction)

    @discord.ui.select(placeholder="Open a card…", row=1,
                       options=[discord.SelectOption(label="…", value="none")])
    async def open_card(self, interaction, select):
        if select.values[0] == "none":
            await interaction.response.defer()
            return
        card = db.get_card(int(select.values[0]))
        if not card:
            await interaction.response.send_message("That card is gone.", ephemeral=True)
            return
        view = CardView(
            self.invoker_id, card, self.owner_name,
            origin=self,                       # so Back restores this exact page
            owner_id=self.owner_id,
        )
        view.message = self.message
        embed = build_card_embed(
            card, self.owner_name,
            copy_number=db.get_card_copy_number(card["collection_id"]),
            owners=db.count_card_owners(card["song_id"]),
            pinned=_is_pinned(self.owner_id, card["collection_id"]),
        )
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.select(
        placeholder="Filter by variant…", row=2,
        options=[discord.SelectOption(label="All variants", value="all")] + [
            discord.SelectOption(label=VARIANT_LABEL[v], value=v,
                                 emoji=safe_option_emoji(variant_emoji(v)))
            for v in VARIANTS
        ],
    )
    async def variant_filter(self, interaction, select):
        self.variant = None if select.values[0] == "all" else select.values[0]
        self.page = 0
        self.reload()
        await self._refresh(interaction)

    @discord.ui.select(
        placeholder="Sort…", row=3,
        options=[discord.SelectOption(label=label, value=key) for key, label in SORTS.items()],
    )
    async def sort_by(self, interaction, select):
        self.sort = select.values[0]
        self.page = 0
        self.reload()
        await self._refresh(interaction)


def _is_pinned(user_id, collection_id):
    profile = db.get_user_profile(user_id)
    return bool(profile and profile["pinned_collection_id"] == collection_id)
