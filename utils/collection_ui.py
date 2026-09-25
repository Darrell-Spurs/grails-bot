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
import asyncio
import logging

import discord

import db
from utils.card_art import ART_FILENAME, card_art_file
from utils import aesthetics

log = logging.getLogger("grails.ui")

# Vocabulary lives in utils/aesthetics.py; import it from there, not from here.
from utils.aesthetics import (
    RARITY_ORDER, UNASSIGNED, VARIANT_LABEL, VARIANTS,
    card_emoji, rarity_rank, safe_option_emoji, variant_emoji, variant_rank,
)

SORTS = {
    "variant": "Sort by variant",
    "newest": "Newest first",
    "artist": "Artist A-Z",
    "song": "Song A-Z",
}

PER_PAGE = 10
TIMEOUT = 180


def rarity_colour(rarity):
    """discord.Colour for a rarity, from the shared palette."""
    return discord.Colour(aesthetics.rarity_colour_int(rarity))


# ---------------------------------------------------------------------------
# Card embed
# ---------------------------------------------------------------------------

def build_card_embed(card, owner_name, copy_number=None, owners=None, pinned=False,
                     art_filename=None, owner_icon=None):
    """One collected card. Album art comes straight from the stored Spotify URL,
    so nothing is rendered and the embed is instant."""
    rarity = (card["rarity"] or UNASSIGNED).lower()
    variant = (card["variant"] or "default").lower()

    title = f"{card_emoji(rarity, variant)} {card['song_name']}"
    if variant == "mythic" and copy_number:
        title += f"  ·  #{copy_number}"

    embed = discord.Embed(title=title, colour=rarity_colour(rarity))
    # set_author rejects icon_url=None, but is happy for it to be absent.
    author = {"name": f"{owner_name}'s card"}
    if owner_icon:
        author["icon_url"] = owner_icon
    embed.set_author(**author)
    embed.description = f"by **{card['artist'] or '—'}** from *{card['album_name'] or '—'}*"

    # A variant with its own artwork is uploaded alongside the embed; everything
    # else points straight at Spotify, which costs no upload.
    if art_filename:
        embed.set_image(url=f"attachment://{art_filename}")
    elif card.get("album_image"):
        embed.set_image(url=card["album_image"])

    footer = f"Card #{card['collection_id']}"
    if pinned:
        footer += "  ·  \U0001F4CC pinned to profile"
    if card.get("collected_at"):
        footer += f"  ·  pulled {_short_time(card['collected_at'])}"
    embed.set_footer(text=footer)
    return embed


async def render_card(card, owner_name, *, owner_icon=None, pinned=None, owner_id=None):
    """A card screen: (embed, art file or None).

    Every card screen -- /view, a card opened from a collection, the pinned card
    on a profile, and the re-render after pinning -- used to assemble this
    itself: art in a worker thread, then three separate queries on the event
    loop for the copy number, owner count and pin state. Those are one query
    now, run in a thread *alongside* the art, so the database time hides behind
    the render instead of adding to it and no longer stalls the bot.

    Pass `pinned` when the caller already knows (a pinned card, a fresh pin);
    otherwise pass `owner_id` and it is looked up in the same query. The mythic
    copy number is stored on the card row, so it arrives with the card.
    """
    lookup_owner = owner_id if pinned is None else None
    art, (owners, looked_up) = await asyncio.gather(
        asyncio.to_thread(card_art_file, card),
        asyncio.to_thread(db.get_card_screen_stats, card["collection_id"],
                          card["song_id"], lookup_owner),
    )
    embed = build_card_embed(
        card, owner_name,
        copy_number=card.get("copy_number"),
        owners=owners,
        pinned=bool(looked_up) if pinned is None else pinned,
        art_filename=ART_FILENAME if art else None,
        owner_icon=owner_icon,
    )
    return embed, art


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
                           artist=None, sort="variant", owner_icon=None):
    """The collection page.

    Whose collection it is lives in the author line with their avatar, so the
    header reads the same whatever filters are on. An artist filter goes in the
    footer rather than the heading: it changes what is listed, not whose list
    it is.
    """
    lines = [_card_line(page * PER_PAGE + i + 1, c) for i, c in enumerate(cards)]

    # The colour bar takes the rarest tier on the page, so scrolling towards
    # the good stuff is visible before reading a word.
    top = min((rarity_rank(c["rarity"]) for c in cards), default=len(RARITY_ORDER))
    colour = rarity_colour(RARITY_ORDER[top]) if top < len(RARITY_ORDER) else discord.Colour(0x5B4BDB)

    embed = discord.Embed(
        description="\n".join(lines) if lines else "_Nothing here with those filters._",
        colour=colour,
    )

    bits = [f"Page {page + 1}/{max(pages, 1)}", f"{total} {'card' if total == 1 else 'cards'}"]
    if artist:
        bits.append(artist)
    if variant:
        bits.append(f"{VARIANT_LABEL.get(variant, variant)}")
    bits.append(SORTS.get(sort, sort).lower())

    # set_author rejects icon_url=None, but is happy for it to be absent.
    author = {"name": f"{owner_name}'s collection"}
    if owner_icon:
        author["icon_url"] = owner_icon
    embed.set_author(**author)
    embed.set_footer(text=" · ".join(bits))
    return embed


def load_collection_cards(owner_id, variant=None, artist=None):
    """One player's cards for a collection view (blocking: run it in a thread).

    Each mythic's copy number arrives on its row; this used to take a second
    query to work them out for the page.
    """
    cards = db.search_user_cards(owner_id, query="", variant=variant, limit=1000)
    if artist:
        wanted = artist.lower()
        cards = [c for c in cards if (c["artist"] or "").lower() == wanted]
    return cards


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

class DisableOnTimeoutView(discord.ui.View):
    """A view that greys out its controls when it expires, so a stale message
    stops offering buttons that would only fail. Set `message` after sending.

    Three views -- this module's, the profile's and the catalog paginator --
    each carried an identical copy of this.
    """

    message = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class _OwnerView(DisableOnTimeoutView):
    """Base view that only lets the invoking user drive the components."""

    def __init__(self, invoker_id, timeout=TIMEOUT):
        super().__init__(timeout=timeout)
        self.invoker_id = int(invoker_id)
        self.message = None
        self.back_to = None
        # Items are built base-class-first, so this button would otherwise sit
        # to the *left* of the subclass's own controls. Re-adding moves it to
        # the end of its row, after Prev/Next.
        self.remove_item(self.back_to_profile)
        self.add_item(self.back_to_profile)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "That menu belongs to someone else — run the command yourself to get your own.",
                ephemeral=True,
            )
            return False
        return True

    # Defined on the base so the collection list and a single card share one
    # implementation. Subclasses that are not reached from a profile drop it in
    # __init__, so it only ever appears when there is somewhere to go back to.
    @discord.ui.button(label="Back to profile",
                       style=discord.ButtonStyle.secondary, row=0)
    async def back_to_profile(self, interaction, button):
        # `back_to` is async: rebuilding the profile reads the database.
        rebuilt = await self.back_to() if self.back_to else None
        if rebuilt is None:
            await interaction.response.send_message(
                "That profile is no longer available.", ephemeral=True)
            return
        embed, view = rebuilt
        view.message = self.message
        await interaction.response.edit_message(embed=embed, view=view)


class TradePartnerSelect(discord.ui.UserSelect):
    """Pick who to trade a specific card with.

    A UserSelect rather than a text box: it resolves to a real member, so there
    is no name to mistype and no ambiguity between two people called the same
    thing. It cannot live in a modal -- Discord only allows text inputs there --
    so the button opens this as an ephemeral view instead.
    """

    def __init__(self, card):
        self.card = card
        super().__init__(placeholder="Who do you want to trade with?",
                         min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        cog = interaction.client.get_cog("TradeCog")
        if cog is None:
            await interaction.response.edit_message(
                content="Trading is unavailable right now.", view=None)
            return

        # Re-read the row instead of rebuilding it from the card dict: the query
        # is scoped to the owner, so it doubles as the check that this card is
        # still theirs after however long the card embed has been on screen.
        row = await asyncio.to_thread(
            db.get_collection_item_by_id, self.card["collection_id"], interaction.user.id)
        if row is None:
            await interaction.response.edit_message(
                content="That card is no longer yours.", view=None)
            return

        await interaction.response.edit_message(
            content=f"Starting a trade with **{target.display_name}**…", view=None)
        problem = await cog.start_trade(interaction.channel, interaction.user, target, row)
        if problem:
            await interaction.edit_original_response(content=problem)


class TradePartnerView(discord.ui.View):
    """The ephemeral partner picker behind the Trade this button."""

    def __init__(self, invoker_id, card, *, timeout=120):
        super().__init__(timeout=timeout)
        self.invoker_id = str(invoker_id)
        self.add_item(TradePartnerSelect(card))

    async def interaction_check(self, interaction: discord.Interaction):
        return str(interaction.user.id) == self.invoker_id


class CardView(_OwnerView):
    """A single card, with the actions that make sense for who is looking."""

    def __init__(self, invoker_id, card, owner_name, *, origin=None, owner_id=None,
                 back_to=None, owner_icon=None):
        super().__init__(invoker_id)
        # `back_to` is the profile this card was opened from; `origin` is a
        # collection page. A card reached through the collection keeps the
        # collection's own Back, so only one of the two is ever in play.
        self.back_to = back_to
        self.card = card
        self.owner_name = owner_name
        self.owner_id = str(owner_id if owner_id is not None else card["user_id"])
        self.owner_icon = owner_icon
        # `origin` is the collection state to restore; None when /view was
        # called directly, in which case there is nothing to go back to.
        self.origin = origin

        is_owner = str(invoker_id) == self.owner_id

        if origin is None:
            self.remove_item(self.back)
        if back_to is None:
            self.remove_item(self.back_to_profile)
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
        # Opens the trade for real rather than printing a command to retype.
        # The one thing the card cannot supply is a counterparty, so that is all
        # this asks for; everything else is already known.
        await interaction.response.send_message(
            f"Trading **{self.card['song_name']}** by {self.card['artist']}.",
            view=TradePartnerView(self.invoker_id, self.card),
            ephemeral=True,
        )

    @discord.ui.button(label="Pin to profile", emoji="\U0001F4CC",
                       style=discord.ButtonStyle.secondary, row=0)
    async def pin_card(self, interaction, button):
        ok = await asyncio.to_thread(db.set_pinned_card, self.invoker_id, self.card["collection_id"])
        if not ok:
            await interaction.response.send_message(
                "Could not pin that card — register with `.register` first.", ephemeral=True)
            return
        embed, art = await render_card(self.card, self.owner_name,
                                       owner_icon=self.owner_icon, pinned=True)
        await interaction.response.edit_message(
            embed=embed, view=self, attachments=[art] if art else [])
        await interaction.followup.send(
            f"\U0001F4CC Pinned **{self.card['song_name']}** by {self.card['artist']} to your profile.", ephemeral=True)

    @discord.ui.button(label="View artist", style=discord.ButtonStyle.secondary, row=0)
    async def view_artist(self, interaction, button):
        # Shows the card owner's collection for that artist, not the viewer's --
        # otherwise clicking it on someone else's card would jump contexts.
        view = await CollectionView.open(
            invoker_id=self.invoker_id,
            owner_id=self.owner_id,
            owner_name=self.owner_name,
            owner_icon=self.owner_icon,
            artist=self.card["artist"],
        )
        view.message = self.message
        await interaction.response.edit_message(embed=view.render(), view=view)


class CollectionView(_OwnerView):
    """Paginated collection list with variant/sort controls and a card opener."""


    async def interaction_check(self, interaction):
        """Anyone may browse, page and filter a collection.

        Reading somebody's collection is already public -- /collection takes a
        user argument -- so locking the pager only forced a bystander to re-run
        the command to see the same cards. The actions that change anything live
        on CardView, which stays locked to its own invoker.
        """
        return True

    def __init__(self, invoker_id, owner_id, owner_name, *, cards, variant=None,
                 artist=None, sort="variant", back_to=None, owner_icon=None):
        """Build the view around cards already loaded -- use `await
        CollectionView.open(...)`, which loads them off the event loop.

        Loading used to happen in here, but a discord.py View can only be
        created on the event loop's thread, so the database read cannot move
        to a worker from inside the constructor.
        """
        super().__init__(invoker_id)
        # An async callable returning (embed, view) for whatever opened this
        # list, or None when /collection was run directly and there is nowhere
        # to go.
        self.back_to = back_to
        self.owner_id = str(owner_id)
        self.owner_name = owner_name
        self.owner_icon = owner_icon
        self.variant = variant
        self.artist = artist
        self.sort = sort
        self.page = 0
        self.all_cards = sort_cards(cards, sort)
        if back_to is None:
            self.remove_item(self.back_to_profile)

    @classmethod
    async def open(cls, invoker_id, owner_id, owner_name, *, variant=None, artist=None, **kwargs):
        """Load the collection in a worker thread, then build the view."""
        cards = await asyncio.to_thread(load_collection_cards, owner_id, variant, artist)
        return cls(invoker_id, owner_id, owner_name, cards=cards,
                   variant=variant, artist=artist, **kwargs)

    # ---- data ----------------------------------------------------------
    async def reload(self):
        """Re-read the cards after the variant filter changes."""
        cards = await asyncio.to_thread(load_collection_cards, self.owner_id,
                                        self.variant, self.artist)
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
            owner_icon=self.owner_icon,
        )

    def _sync_components(self):
        self.prev_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.pages - 1

        opener = self.open_card
        opener.options = [
            discord.SelectOption(
                label=c['song_name'][:100],
                description=c['artist'][:100],
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
        card = await asyncio.to_thread(db.get_card, int(select.values[0]))
        if not card:
            await interaction.response.send_message("That card is gone.", ephemeral=True)
            return
        # Keyed to the clicker, not the original invoker: anyone can drive this
        # list now, and a stranger who opens a card must be able to use Back.
        # CardView decides for itself which buttons a non-owner may see.
        view = CardView(
            interaction.user.id, card, self.owner_name,
            origin=self,                       # so Back restores this exact page
            owner_id=self.owner_id,
        )
        view.message = self.message
        embed, art = await render_card(card, self.owner_name, owner_icon=self.owner_icon,
                                       owner_id=self.owner_id)
        # attachments=[] clears the list page's own upload, so a plain-cover
        # card cannot inherit the previous screen's picture.
        await interaction.response.edit_message(
            embed=embed, view=view, attachments=[art] if art else [])

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
        await self.reload()
        await self._refresh(interaction)

    @discord.ui.select(
        placeholder="Sort…", row=3,
        options=[discord.SelectOption(label=label, value=key) for key, label in SORTS.items()],
    )
    async def sort_by(self, interaction, select):
        # Re-sorts the cards already loaded: a new order needs no new read.
        self.sort = select.values[0]
        self.page = 0
        self.all_cards = sort_cards(self.all_cards, self.sort)
        await self._refresh(interaction)
