import os, sys

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from db import (get_user_song_by_details, transfer_collection_item, user_exists,
                get_user_mythic_copy_number, get_user_tradeable_items, get_collection_item_by_id)
from utils.command_types import slash_only
from utils.errors import report_unhandled
from utils.aesthetics import ACCENT_COLOR_INT, VARIANTS, VARIANT_LABEL, card_emoji, safe_option_emoji

log = logging.getLogger("grails.trade")

# These are collection *variants*, not song rarities -- the local name is kept
# so the rest of this cog reads unchanged.
RARITIES = VARIANTS

# How long an unanswered trade request stands before it cancels itself.
TRADE_TIMEOUT = 120

# With no filter set, how much has to be typed before the item list appears.
MIN_AUTOCOMPLETE_CHARS = 2

# Index of the collection variant in a resolved trade row. The rows come from
# get_user_song_by_details / get_user_tradeable_items, which agree on:
# (collection_id, song_id, album_id, song_name, artist, variant, album_name, rarity)
_VARIANT = 5


def _variant_of(row):
    """The variant a resolved card actually is.

    Everything downstream reads the variant off the row rather than off the
    `rarity` argument. That argument is only a *filter* now -- it can be absent,
    and the two sides of a trade need not agree on it -- so trusting it to
    describe the card would remove the wrong row from a collection.
    """
    return row[_VARIANT]



def _find_trade_item(user_id, rarity, item_input, artist=None):  # rarity may be None
    """Resolve a trade item.

    - If `item_input` is a collections.id (what slash autocomplete sends), look it up directly
      and verify ownership + rarity.
    - Otherwise, treat it as free text (prefix-command fallback) and do a single indexed query
      + substring match instead of the old O(n^2) split-guessing. `artist`, if given, narrows the
      candidate set first (useful when a user has too many songs of a rarity to browse otherwise).

    Returns (row_or_None, candidate_matches). `row` uses the same column order as
    get_user_song_by_details: (collection_id, song_id, album_id, song_name, artist, variant, album_name).
    """
    item_input = (item_input or "").strip()

    if item_input.isdigit():
        row = get_collection_item_by_id(int(item_input), user_id)
        if row and (rarity is None or row[_VARIANT] == rarity):
            return row, []

    candidates = get_user_tradeable_items(user_id, rarity, artist=artist)
    needle = item_input.lower()
    matches = [c for c in candidates if needle in c[3].lower()]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


async def _rarity_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    return [app_commands.Choice(name=r.title(), value=r) for r in RARITIES if cur in r][:25]


async def _own_artist_autocomplete(interaction: discord.Interaction, current: str):
    """The distinct artists the user owns a song by, for narrowing `item`.

    As with the item box, `rarity` narrows this rather than gating it: with none
    chosen yet the list is every artist in the collection, the same set /view
    offers. Blanking it instead made the first box a player touches look like an
    empty collection.
    """
    rarity = (getattr(interaction.namespace, "rarity", None) or "").lower()
    rarity = rarity if rarity in RARITIES else None
    try:
        if rarity:
            items = get_user_tradeable_items(str(interaction.user.id), rarity)
            artists = sorted({row[4] for row in items})
        else:
            artists = db.get_user_collection_artists(interaction.user.id)
    except Exception:
        # An autocomplete that raises shows the user an empty box and logs
        # nothing, which is indistinguishable from owning no cards.
        log.exception("trade artist autocomplete failed")
        return []
    cur = (current or "").lower()
    return [app_commands.Choice(name=a[:100], value=a[:100])
            for a in artists if cur in a.lower()][:25]


async def _own_item_autocomplete(interaction: discord.Interaction, current: str):
    """A song from the invoker's own collection, narrowed by the `rarity` (and,
    if given, `artist`) already chosen on this invocation.

    Shares /view's flow, and its `db.search_user_cards` query, so the two
    surfaces resolve the same text to the same card. The search runs in the
    database with a LIMIT instead of pulling the whole collection back and
    substring-matching it here -- the old version also matched against the
    assembled `"<song> by <artist> from <album>"` label, so typing "by" or
    "from" matched every card the player owned.

    Either filter is enough on its own to show the list. `rarity` is a filter
    here, not a prerequisite: an unset or half-typed one simply does not narrow
    the search, rather than blanking it -- the box is often the first one a
    player touches, and refusing to answer it looks like an empty collection.
    """
    rarity = (getattr(interaction.namespace, "rarity", None) or "").lower()
    rarity = rarity if rarity in RARITIES else None
    artist = getattr(interaction.namespace, "artist", None) or None
    query = (current or "").strip()

    # With nothing narrowing it, wait for a couple of characters: an unfiltered
    # collection runs to hundreds of cards, and an arbitrary 25 of them tells
    # the player nothing.
    if not (rarity or artist) and len(query) < MIN_AUTOCOMPLETE_CHARS:
        return []

    try:
        cards = db.search_user_cards(interaction.user.id, query=query, limit=25,
                                     variant=rarity, artist=artist)
    except Exception:
        log.exception("trade item autocomplete failed")
        return []

    def _label(card):
        line = f"{card['song_name']} by {card['artist']}"
        # No rarity picked means the results span every variant. Naming it keeps
        # the pick honest: choosing a vinyl here and then `default` in the rarity
        # box would fail to resolve, with nothing on screen to explain why.
        return line if rarity else f"[{card['variant']}] {line}"

    return [
        app_commands.Choice(name=_label(c)[:100], value=str(c["collection_id"]))
        for c in cards
    ]


# ---------------------------------------------------------------------------
# The Offer button on a trade request
#
# A select menu holds at most 25 options and takes no typed input, so the picker
# pairs one with a "Search by name" button that opens a modal. A modal is the
# only Discord surface that accepts free text, and it cannot itself contain a
# select -- hence the two-step: type to narrow, then pick from what came back.
# ---------------------------------------------------------------------------

# Discord's hard cap on select options.
SELECT_LIMIT = 25


def _mentions(*users):
    """The mentions to carry as a message's content.

    A mention inside an embed renders as a link but never raises a
    notification -- only message *content* does. So the embeds name people in
    bold, and the same message carries the bare mentions as its content, where
    Discord will actually act on them. Content renders directly above the
    embed, so this reads as one message rather than two.
    """
    return " ".join(u.mention for u in users)


def _card_option(row):
    """One select option for a collection row."""
    return discord.SelectOption(
        label=row[3][:100],
        value=str(row[0]),
        # The variant and album, which is what tells two copies of one song
        # apart in a list that shows every variant at once.
        description=f"{row[4]} · {VARIANT_LABEL.get(_variant_of(row), _variant_of(row))}"[:100],
        emoji=safe_option_emoji(card_emoji(row[7] if len(row) > 7 else None,
                                          (_variant_of(row) or "").lower())),
    )


def _newest_first(rows):
    """Highest collection id first -- the id is an identity, so it orders by
    insertion without depending on collected_at, which ties on a fast run."""
    return sorted(rows, key=lambda r: r[0], reverse=True)


class OfferSelect(discord.ui.Select):
    """The card dropdown itself."""

    def __init__(self, picker, rows):
        self.picker = picker
        self.rows = {str(r[0]): r for r in rows}
        super().__init__(placeholder="Pick a card to offer…",
                         min_values=1, max_values=1,
                         options=[_card_option(r) for r in rows])

    async def callback(self, interaction: discord.Interaction):
        await self.picker.submit(interaction, self.rows[self.values[0]])


class OfferSearchModal(discord.ui.Modal, title="Find a card to offer"):
    """The typed-input escape hatch for collections past 25 cards."""

    query = discord.ui.TextInput(label="Enter Keywords", required=True, max_length=100,
                                 placeholder="Search by part of the title or artist")

    def __init__(self, picker):
        super().__init__()
        self.picker = picker

    async def on_submit(self, interaction: discord.Interaction):
        await self.picker.search(interaction, str(self.query))


class OfferPicker(discord.ui.View):
    """Ephemeral card picker, shown to the trade's target when they click Offer."""

    def __init__(self, cog, trade_key, responder, rows, *, timeout=120):
        super().__init__(timeout=timeout)
        self.cog, self.trade_key, self.responder = cog, trade_key, responder
        self.rebuild(rows)

    def rebuild(self, rows):
        self.clear_items()
        if rows:
            self.add_item(OfferSelect(self, rows[:SELECT_LIMIT]))
        self.add_item(_SearchButton())

    async def interaction_check(self, interaction: discord.Interaction):
        # The picker is ephemeral, so only its owner can see it -- but a check
        # still belongs here: an ephemeral message survives in the client and
        # can be clicked long after the trade it belonged to has gone.
        return interaction.user.id == self.responder.id

    def _trade(self):
        return self.cog.active_trades.get(self.trade_key)

    async def search(self, interaction, text):
        rows = await asyncio.to_thread(get_user_tradeable_items, str(self.responder.id))
        needle = text.strip().lower()
        hits = [r for r in rows if needle in r[3].lower() or needle in r[4].lower()]
        if not hits:
            await interaction.response.edit_message(
                content=f"None of your cards matches **{text}**. Try again.",
                view=self)
            return
        self.rebuild(_newest_first(hits))
        shown = min(len(hits), SELECT_LIMIT)
        more = f" (showing the {shown} newest)" if len(hits) > SELECT_LIMIT else ""
        await interaction.response.edit_message(
            content=f"**{len(hits)}** matched **{text}**{more}.", view=self)

    async def submit(self, interaction, row):
        if self._trade() is None:
            await interaction.response.edit_message(
                content="❌ That trade is no longer open.", view=None)
            return
        # Answer the interaction before running the trade: the confirmation
        # waits on reactions for up to a minute, far past the 3s Discord allows.
        await interaction.response.edit_message(
            content=f"Offered **{row[3]}**. React on the trade message to confirm.",
            view=None)
        trade = self._trade()
        await self.cog._run_offer(self.responder, self.trade_key, trade, row)


class _SearchButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Search for more cards", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(OfferSearchModal(self.view))


class TradeRequestView(discord.ui.View):
    """The Offer button that rides on a posted trade request."""

    def __init__(self, cog, trade_key, target):
        # No timeout of its own: the trade's own expiry timer owns the lifetime,
        # and a view that died first would leave a dead button on a live trade.
        super().__init__(timeout=None)
        self.cog, self.trade_key, self.target = cog, trade_key, target

    @discord.ui.button(label="Offer a card", style=discord.ButtonStyle.primary)
    async def offer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message(
                "This trade request isn't for you.", ephemeral=True)
            return
        if self.cog.active_trades.get(self.trade_key) is None:
            await interaction.response.send_message(
                "That trade is no longer open.", ephemeral=True)
            return

        rows = await asyncio.to_thread(get_user_tradeable_items, str(interaction.user.id))
        if not rows:
            await interaction.response.send_message(
                "You have no cards to offer yet — pull some with `.c`.", ephemeral=True)
            return

        rows = _newest_first(rows)
        note = (f"Your newest {SELECT_LIMIT} cards. Use **Search for more cards** for the rest ")
        await interaction.response.send_message(
            note, view=OfferPicker(self.cog, self.trade_key, interaction.user, rows),
            ephemeral=True)


class GiftConfirmView(discord.ui.View):
    """Send / Cancel on a pending gift, usable only by the gifter.

    A gift hands a card over with no counter-offer and no undo, so unlike a
    trade there is nothing downstream to catch a mistake -- and with `item`
    optional, `/gift @someone` on its own resolves to whatever was pulled most
    recently, which can be a mythic.

    Nothing moves until Send is pressed: the pending message is a proposal, not
    a receipt.
    """

    def __init__(self, gifter, receiver, row, line, *, timeout=120):
        super().__init__(timeout=timeout)
        self.gifter, self.receiver = gifter, receiver
        self.row, self.line = row, line
        self.message = None
        self._settled = False

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.gifter.id:
            await interaction.response.send_message(
                "This gift isn't yours to send.", ephemeral=True)
            return False
        return True

    def _close(self):
        """Take the view out of play. False if it already was.

        The transfer is awaited, so without this a double-click could enter the
        Send handler twice before the first one finished.
        """
        if self._settled:
            return False
        self._settled = True
        for item in self.children:
            item.disabled = True
        self.stop()
        return True

    @discord.ui.button(label="Send", style=discord.ButtonStyle.success)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._close():
            return
        # Acknowledge first: the transfer is a database round trip and Discord
        # allows three seconds.
        await interaction.response.defer()

        moved = await asyncio.to_thread(
            transfer_collection_item, self.row[0], self.gifter.id, self.receiver.id)
        if not moved:
            await interaction.message.edit(
                embed=discord.Embed(
                    title="Gift failed",
                    description="That card is no longer yours — it may have "
                                "just been traded away.",
                    colour=discord.Colour.red()),
                view=None)
            return

        embed = discord.Embed(
            title="Gift sent 🎁",
            description=f"**{self.gifter.display_name}** gifted "
                        f"**{self.receiver.display_name}**:\n\n{self.line}",
            colour=discord.Colour(ACCENT_COLOR_INT),
        )
        # Posted fresh rather than edited into the pending message: Discord
        # raises no notification for a mention added by an edit, so editing in
        # place would leave the receiver unpinged for a card they now own.
        await interaction.channel.send(content=_mentions(self.receiver), embed=embed)
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            await interaction.message.edit(view=None)
        log.info("gift: %s -> %s  %s [%s]", self.gifter, self.receiver,
                 self.row[3], _variant_of(self.row))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._close():
            return
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Gift cancelled",
                description=f"Nothing was given away.\n\n{self.line}",
                colour=discord.Colour.red()),
            view=None)

    async def on_timeout(self):
        if not self._close() or self.message is None:
            return
        try:
            await self.message.edit(
                embed=discord.Embed(
                    title="Gift expired",
                    description=f"Nobody confirmed, so the card stayed put."
                                f"\n\n{self.line}",
                    colour=discord.Colour.red()),
                view=None)
        except discord.HTTPException:
            pass


class TradeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active_trades = {}  # Store active trade requests

    def _drop_trade(self, key):
        """Remove a trade and stop its expiry timer. Returns the trade, or None."""
        data = self.active_trades.pop(key, None)
        if data is None:
            return None
        timer = data.get("timer")
        # Cancelling the task we are running *inside* would throw CancelledError
        # straight back through this call, so a timer never cancels itself.
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        return data

    async def _close_trade(self, key, reason):
        """End a trade and say why on its message.

        Shared by /canceltrade and the expiry timer so the two cannot drift. An
        expired trade used to keep its original embed with only the footer
        changed, while a cancelled one was replaced outright -- the same outcome
        looked like two different things.
        """
        data = self._drop_trade(key)
        if data is None:
            return False
        embed = discord.Embed(
            title="❌ Trade Cancelled",
            description=f"**{data['target'].display_name}** {reason}.",
            color=discord.Color.red(),
        )
        view = data.get("view")
        if view is not None:
            view.stop()
        try:
            # view=None strips the Offer button; a cancelled trade that still
            # showed one would hand the target a button that only errors.
            await data["message"].edit(embed=embed, view=None)
        except discord.HTTPException:
            log.exception("could not edit the trade message for %s", key)
        return True

    async def _expire_trade(self, key, token):
        """Cancel a trade nobody answered inside TRADE_TIMEOUT."""
        try:
            await asyncio.sleep(TRADE_TIMEOUT)
        except asyncio.CancelledError:
            return
        data = self.active_trades.get(key)
        # A newer trade may hold this key by now -- only expire our own.
        if data is None or data.get("token") is not token:
            return
        await self._close_trade(key, f"expired after {TRADE_TIMEOUT // 60} minutes")

    def format_song_display(self, user_id, song_data):
        """Format song display with copy number for mythics.

        The variant comes off the row, not from the caller: with `rarity`
        optional there may be no argument to pass, and the two sides of a trade
        can now hold different variants entirely.
        """
        song_name = song_data[3]
        artist_name = song_data[4]
        album_name = song_data[6]
        song_rarity = song_data[7] if len(song_data) > 7 else None
        variant = (_variant_of(song_data) or "").lower()
        glyph = card_emoji(song_rarity, variant)

        if variant == "mythic":
            copy_number = get_user_mythic_copy_number(user_id, song_data[1])  # song_data[1] is song_id
            if copy_number:
                return (f"{glyph} ** #{copy_number} {song_name}** by **{artist_name}** from *{album_name}*")
        return f"{glyph} **{song_name}** by **{artist_name}**"

    def _ambiguous_message(self, rarity, item, matches, artist=None):
        # `rarity` is optional now, so every phrase has to read without it.
        rarity_note = f"`{rarity}` " if rarity else ""
        artist_note = f" by **{artist}**" if artist else ""
        if matches:
            options = "\n".join(f"- {m[3]} - {m[4]} ({m[6]})" for m in matches[:10])
            more = f"\n…and {len(matches) - 10} more" if len(matches) > 10 else ""
            return (f"❌ **Multiple {rarity_note}songs{artist_note} match `{item}`** — be more specific, "
                    f"or be more specific:\n{options}{more}")
        if not (item or "").strip():
            # No item was named, so nothing "matched" -- the user simply owns
            # nothing that fits the filters.
            return (f"❌ **You don't have any {rarity_note}songs{artist_note} to offer.**\n"
                    f"Pull some with `.c`, or widen the filters.")
        return (f"❌ **You don't have a {rarity_note}song{artist_note} matching `{item}`.**\n"
                f"Use `/trade` for autocomplete of your collection (with an artist filter "
                f"if you have a lot of songs).")

    async def _resolve_trade_item(self, user_id, rarity, artist, item):
        """Resolve (artist, item) into a collection row, with a prefix-mode fallback: if the
        first word of a free-text song name got mistakenly parsed into `artist` (prefix commands
        have no autocomplete to keep the two apart), retry treating the whole thing as `item`."""
        # No item named: take the newest card of that variant. Highest collection
        # id is the most recent -- the column is an identity, so it orders by
        # insertion without needing collected_at, which only has second
        # resolution and ties on a fast run of pulls.
        if not (item or "").strip():
            owned = await asyncio.to_thread(get_user_tradeable_items, user_id, rarity, artist)
            if not owned:
                return None, [], artist
            return max(owned, key=lambda r: r[0]), [], artist

        song_found, matches = await asyncio.to_thread(_find_trade_item, user_id, rarity, item, artist)
        if song_found is None and artist:
            combined = f"{artist} {item}".strip() if item else artist
            retry_found, retry_matches = await asyncio.to_thread(_find_trade_item, user_id, rarity, combined, None)
            # Adopt the retry if it resolved cleanly, or if the "artist" filter zeroed out every
            # candidate (a strong signal it was actually the start of the song name, not a real filter).
            if retry_found is not None or not matches:
                return retry_found, retry_matches, None
        return song_found, matches, artist

    @commands.hybrid_command(name="trade", description="Offer a song to trade (default to the most recent song)")
    @slash_only()
    @app_commands.describe(user="User to trade with", rarity="Optional: only offer cards of this variant",
                           artist="Optional: only cards by this artist",
                           item="The song to offer (search with song title or artist)")
    @app_commands.autocomplete(rarity=_rarity_autocomplete, artist=_own_artist_autocomplete, item=_own_item_autocomplete)
    async def trade(self, ctx, user: discord.Member, rarity: Optional[str] = None, *,
                    artist: Optional[str] = None, item: Optional[str] = None):
        """Start a trade with another user.
        Usage: .trade @user [rarity] [song] — or use /trade for autocomplete (+ optional artist filter).
        """
        # A filter, not a requirement: absent means "any variant". Only a value
        # that was actually typed and is not a variant is an error.
        rarity = (rarity or "").lower() or None
        if rarity is not None and rarity not in RARITIES:
            await ctx.send(f"❌ **Invalid Rarity: `{rarity}`**\n"
                           f"**Valid Rarities:** {', '.join(f'`{r}`' for r in RARITIES)}")
            return

        song_found, matches, artist = await self._resolve_trade_item(
            str(ctx.author.id), rarity, artist, item)
        if song_found is None:
            await ctx.send(self._ambiguous_message(rarity, item, matches, artist))
            return

        problem = await self.start_trade(ctx.channel, ctx.author, user, song_found)
        if problem:
            await ctx.send(problem)

    async def start_trade(self, channel, initiator, target, song_found):
        """Post a trade request for `song_found`. Returns a complaint, or None.

        Split out of /trade so the "Trade this" button on a card can open a
        trade without reproducing any of this. Everything the trade needs is
        here -- the eligibility checks included -- because a caller that only
        has a card and two users has no way to run them itself.
        """
        if target.id == initiator.id:
            return "❌ **You can't trade with yourself!**"
        if target.bot:
            return "❌ **You can't trade with bots!**"
        if not await asyncio.to_thread(user_exists, target.id):
            return f"❌ **{target.display_name}** is not registered yet."

        trade_key = f"{initiator.id}_{target.id}"
        reverse_trade_key = f"{target.id}_{initiator.id}"
        if trade_key in self.active_trades or reverse_trade_key in self.active_trades:
            return ("❌ There's already an active trade between you two!"
                    "\n**Tip:** Use `/canceltrade` to cancel the existing trade.")

        embed = discord.Embed(
            title="🔄 Trade Request",
            description=f"**{initiator.display_name}** wants to trade with "
                        f"**{target.display_name}**",
            color=discord.Color.blue()
        )
        embed.add_field(
            name=f"{initiator.display_name} offers:",
            value=self.format_song_display(str(initiator.id), song_found),
            inline=False
        )
        embed.add_field(
            name=f"{target.display_name} offers:",
            value="Pending — click **Offer a card** below",
            inline=False
        )
        embed.set_footer(text="Use /canceltrade or .ct to cancel the trade.")

        # The view only stores the key, so it can be built before the trade is
        # registered a few lines below -- it looks the trade up on each click
        # rather than holding a reference that could go stale.
        view = TradeRequestView(self, trade_key, target)
        trade_msg = await channel.send(content=_mentions(target), embed=embed, view=view)

        token = object()
        self.active_trades[trade_key] = {
            'initiator': initiator,
            'target': target,
            'initiator_song': song_found,
            # The variant of the card actually offered, not the filter that
            # found it -- this is what gets removed from the collection.
            'rarity': _variant_of(song_found),
            'message': trade_msg,
            'view': view,
            'token': token,
        }
        self.active_trades[trade_key]["timer"] = asyncio.create_task(
            self._expire_trade(trade_key, token))
        return None

    @commands.hybrid_command(name="gift", description="Gift a song (default to the most recent song)")
    @slash_only()
    @app_commands.describe(user="Who to gift it to", rarity="Optional: only gift cards of this variant",
                           artist="Optional: only cards by this artist",
                           item="The song to gift (search with song title or artist)")
    @app_commands.autocomplete(rarity=_rarity_autocomplete, artist=_own_artist_autocomplete,
                               item=_own_item_autocomplete)
    async def gift(self, ctx, user: discord.Member, rarity: Optional[str] = None, *,
                   artist: Optional[str] = None, item: Optional[str] = None):
        """Gift a song to a user.

        One-way and immediate -- unlike /trade there is nothing to accept and
        nothing comes back. Usage: /gift user:<who> [rarity] [item]
        """
        # A filter, not a requirement: absent means "any variant". Only a value
        # that was actually typed and is not a variant is an error.
        rarity = (rarity or "").lower() or None
        if rarity is not None and rarity not in RARITIES:
            await ctx.send(f"❌ **Invalid Rarity: `{rarity}`**\n"
                           f"**Valid Rarities:** {', '.join(f'`{r}`' for r in RARITIES)}")
            return

        if user.id == ctx.author.id:
            await ctx.send("❌ **You can't gift to yourself!**")
            return
        if user.bot:
            await ctx.send("❌ **You can't gift to bots!**")
            return
        if not await asyncio.to_thread(user_exists, user.id):
            await ctx.send(f"❌ **{user.display_name}** is not registered yet.")
            return

        song_found, matches, artist = await self._resolve_trade_item(
            str(ctx.author.id), rarity, artist, item)
        if song_found is None:
            await ctx.send(self._ambiguous_message(rarity, item, matches, artist))
            return

        # Read the display line before the card moves: afterwards the copy
        # number belongs to the recipient, not the giver.
        line = self.format_song_display(str(ctx.author.id), song_found)

        embed = discord.Embed(
            title="Gift pending ⏳",
            description=f"**{ctx.author.display_name}** is about to gift "
                        f"**{user.display_name}**:\n\n{line}",
            colour=discord.Colour(ACCENT_COLOR_INT),
        )
        # No ping yet. The receiver hears about it when it actually lands, not
        # when it is proposed and could still be cancelled.
        view = GiftConfirmView(ctx.author, user, song_found, line)
        view.message = await ctx.send(embed=embed, view=view)

    @gift.error
    async def gift_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Use `/gift user:<who>` — the item defaults "
                           "to your newest card matching the filters.")
            return
        if isinstance(error, (commands.MemberNotFound, commands.BadArgument)):
            await ctx.send("❌ **Invalid user!** Mention someone with @.")
            return
        await report_unhandled(log, ctx, error, command="gift")

    async def _run_offer(self, responder, trade_key, trade_data, song_found):
        """Put `song_found` up against the open trade and run the confirmation.

        Driven only by the Offer button now that /offer is gone. Takes the
        responder rather than a Context because a button callback has an
        Interaction and no Context at all.
        """
        initiator_song = trade_data['initiator_song']
        embed = discord.Embed(
            title="🔄 Trade Confirmation",
            description=f"Trade between **{trade_data['initiator'].display_name}** "
                        f"and **{responder.display_name}**",
            color=discord.Color.gold()
        )
        embed.add_field(
            name=f"{trade_data['initiator'].display_name} offers:",
            value=self.format_song_display(str(trade_data['initiator'].id), initiator_song),
            inline=False
        )
        embed.add_field(
            name=f"{responder.display_name} offers:",
            value=self.format_song_display(str(responder.id), song_found),
            inline=False
        )
        embed.set_footer(text="Both users react with ✅ to confirm the trade, or ❌ to cancel")

        # The button is spent once a card is on the table: leaving it live would
        # let a second offer overwrite the first mid-confirmation.
        view = trade_data.get('view')
        if view is not None:
            view.stop()

        # Posted fresh rather than edited into the request. Discord raises no
        # notification for a mention added by an edit, so editing in place would
        # leave both players unpinged at the one step that needs them both -- and
        # it puts what needs reacting to at the bottom of the channel instead of
        # back wherever the request has scrolled to.
        request = trade_data['message']
        message = await request.channel.send(
            content=_mentions(trade_data['initiator'], responder), embed=embed)
        trade_data['message'] = message
        try:
            await request.delete()
        except discord.HTTPException:
            # Not fatal, but the stale request must at least lose its button so
            # nobody can open a second offer against a trade already in progress.
            log.exception("could not remove the superseded trade request")
            try:
                await request.edit(view=None)
            except discord.HTTPException:
                pass

        await message.add_reaction("✅")
        await message.add_reaction("❌")

        trade_data['target_song'] = song_found
        trade_data['ready_for_confirmation'] = True

        def check(reaction, reactor):
            return (reactor == trade_data['initiator'] or reactor == responder) and \
                   reaction.message.id == trade_data['message'].id and \
                   str(reaction.emoji) in ['✅', '❌']

        confirmations = set()
        cancellations = set()

        try:
            while len(confirmations) < 2 and len(cancellations) == 0:
                reaction, reactor = await self.bot.wait_for('reaction_add', timeout=60.0, check=check)
                if str(reaction.emoji) == '✅':
                    confirmations.add(reactor.id)
                elif str(reaction.emoji) == '❌':
                    cancellations.add(reactor.id)
                    break

            if cancellations:
                embed.color = discord.Color.red()
                embed.set_footer(text="❌ Trade cancelled")
                await trade_data['message'].edit(embed=embed)
                self._drop_trade(trade_key)
                return

            # Reassign both rows rather than deleting and re-inserting them.
            # The old delete/insert pair went through remove_from_collection,
            # whose `WHERE rowid` is SQLite-only and raised UndefinedColumn on
            # Postgres -- and even where it ran, its unordered `LIMIT 1` picked
            # an arbitrary duplicate and the re-insert renumbered mythic copies,
            # so #1 could come back as #3. Moving the row keeps its id, its
            # collected_at and therefore its copy number.
            initiator_id = str(trade_data['initiator'].id)
            responder_id = str(responder.id)
            moved = (transfer_collection_item(initiator_song[0], initiator_id, responder_id)
                     and transfer_collection_item(song_found[0], responder_id, initiator_id))
            if not moved:
                # Either card can have been gifted or traded away while the
                # confirmation sat open.
                embed.color = discord.Color.red()
                embed.set_footer(text="❌ One of these cards is no longer available")
                await trade_data['message'].edit(embed=embed)
                self._drop_trade(trade_key)
                return

            embed.color = discord.Color.green()
            embed.title = "✅ Trade Completed!"
            embed.clear_fields()
            embed.add_field(
                name=f"{trade_data['initiator'].display_name} received:",
                value=self.format_song_display(str(trade_data['initiator'].id), song_found),
                inline=False
            )
            embed.add_field(
                name=f"{responder.display_name} received:",
                value=self.format_song_display(str(responder.id), initiator_song),
                inline=False
            )
            embed.set_footer(text="Trade successful! Check your collections.")
            await trade_data['message'].edit(embed=embed)
            log.info("Trade completed between %s and %s", trade_data['initiator'].id, responder.id)

            self._drop_trade(trade_key)

        except asyncio.TimeoutError:
            embed.color = discord.Color.red()
            embed.set_footer(text="❌ Trade confirmation timed out")
            await trade_data['message'].edit(embed=embed)
            self._drop_trade(trade_key)

    # Not slash_only: the whole point of the `.ct` alias is a two-keystroke way
    # to back out of a trade, and a hybrid command's aliases are only ever
    # reachable through the prefix form.
    @commands.hybrid_command(name="canceltrade", aliases=["ct"],
                             description="Cancel any active trade requests")
    async def canceltrade(self, ctx):
        """Cancel your active trade request"""
        mine = [key for key, data in self.active_trades.items()
                if ctx.author.id in (data['initiator'].id, data['target'].id)]
        for key in mine:
            await self._close_trade(key, "cancelled the trade.")
        cancelled = bool(mine)

        if cancelled:
            await ctx.send("⚠️ Your trade has been cancelled.")
        else:
            await ctx.send("❌ You don't have any active trades to cancel.")

    @trade.error
    async def trade_error(self, ctx, error):
        """Handle errors for the trade command"""
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing arguments!**\n"
                           "**Usage:** `.trade @user [rarity] [song]` — or use `/trade` for autocomplete.")
        elif isinstance(error, commands.MemberNotFound) or isinstance(error, commands.BadArgument):
            await ctx.send("❌ **Invalid user!** Make sure to properly mention (@) a valid user.")
        else:
            raise error

async def setup(bot):
    await bot.add_cog(TradeCog(bot))
