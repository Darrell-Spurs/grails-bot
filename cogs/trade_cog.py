import os, sys

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from db import (add_song_to_collection, get_user_song_by_details, remove_from_collection,
                transfer_collection_item, user_exists,
                get_user_mythic_copy_number, get_user_tradeable_items, get_collection_item_by_id)
from utils.command_types import slash_only
from utils.errors import report_unhandled
from utils.aesthetics import ACCENT_COLOR_INT, VARIANTS, card_emoji

log = logging.getLogger("grails.trade")

# These are collection *variants*, not song rarities -- the local name is kept
# so the rest of this cog reads unchanged.
RARITIES = VARIANTS


def _find_trade_item(user_id, rarity, item_input, artist=None):
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
        if row and row[5] == rarity:
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
    """Autocomplete the distinct artists the user owns a song of, for the given `rarity`.
    Lets a user with too many songs to browse narrow the item list before picking one."""
    rarity = getattr(interaction.namespace, "rarity", None)
    if not rarity or rarity.lower() not in RARITIES:
        return []
    items = get_user_tradeable_items(str(interaction.user.id), rarity.lower())
    artists = sorted({row[4] for row in items})
    cur = current.lower()
    return [app_commands.Choice(name=a, value=a) for a in artists if cur in a.lower()][:25]


async def _own_item_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete a song from the invoking user's own collection, filtered by the
    already-typed `rarity` (and, if given, `artist`) options."""
    rarity = getattr(interaction.namespace, "rarity", None)
    if not rarity or rarity.lower() not in RARITIES:
        return []
    artist = getattr(interaction.namespace, "artist", None) or None
    items = get_user_tradeable_items(str(interaction.user.id), rarity.lower(), artist=artist)
    cur = current.lower()
    choices = []
    for row in items:
        _, _, _, song_name, artist_name, _, album_name = row
        label = f"{song_name} by {artist_name} from {album_name}"
        if cur in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(row[0])))
    return choices[:25]


class TradeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active_trades = {}  # Store active trade requests

    def format_song_display(self, user_id, song_data, rarity):
        """Format song display with copy number for mythics.

        `rarity` here is the *variant* -- the trade flow's long-standing naming.
        The song's actual rarity rides along as the row's last column, so the
        line can show the same card emoji the collection and view embeds use.
        """
        song_name = song_data[3]
        artist_name = song_data[4]
        album_name = song_data[6]
        song_rarity = song_data[7] if len(song_data) > 7 else None
        glyph = card_emoji(song_rarity, rarity.lower())

        if rarity.lower() == "mythic":
            copy_number = get_user_mythic_copy_number(user_id, song_data[1])  # song_data[1] is song_id
            if copy_number:
                return (f"{glyph} **{song_name}** by **{artist_name}** from *{album_name}*"
                        f"  ·  copy **#{copy_number}**")
        return f"{glyph} **{song_name}** by **{artist_name}** from *{album_name}*"

    def _ambiguous_message(self, rarity, item, matches, artist=None):
        artist_note = f" by **{artist}**" if artist else ""
        if matches:
            options = "\n".join(f"- {m[3]} - {m[4]} ({m[6]})" for m in matches[:10])
            more = f"\n…and {len(matches) - 10} more" if len(matches) > 10 else ""
            return (f"❌ **Multiple `{rarity}` songs{artist_note} match `{item}`** — be more specific, "
                    f"or use `/trade`/`/offer` for autocomplete:\n{options}{more}")
        if not (item or "").strip():
            # No item was named, so nothing "matched" -- the user simply owns
            # none of that variant.
            return (f"❌ **You don't have any `{rarity}` songs{artist_note} to offer.**\n"
                    f"Pull some with `.c`, or pick a different rarity.")
        return (f"❌ **You don't have a `{rarity}` song{artist_note} matching `{item}`.**\n"
                f"Use `/trade` or `/offer` for autocomplete of your collection (with an artist filter "
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

    @commands.hybrid_command(name="trade", description="Offer a song to trade with another user")
    @slash_only()
    @app_commands.describe(user="User to trade with", rarity="Rarity of the song you're offering",
                           artist="Filter to one artist first (handy if you have many songs)",
                           item="The song to offer (default: your newest of that rarity)")
    @app_commands.autocomplete(rarity=_rarity_autocomplete, artist=_own_artist_autocomplete, item=_own_item_autocomplete)
    async def trade(self, ctx, user: discord.Member, rarity: str, *, artist: Optional[str] = None,
                    item: Optional[str] = None):
        """Start a trade with another user.
        Usage: .trade @user rarity <song> — or use /trade for autocomplete (+ optional artist filter).
        """
        rarity = (rarity or "").lower()
        if rarity not in RARITIES:
            await ctx.send(f"❌ **Invalid Rarity: `{rarity}`**\n"
                           f"**Valid Rarities:** {', '.join(f'`{r}`' for r in RARITIES)}")
            return

        if user.id == ctx.author.id:
            await ctx.send("❌ **You can't trade with yourself!**")
            return

        if user.bot:
            await ctx.send("❌ **You can't trade with bots!**")
            return

        trade_key = f"{ctx.author.id}_{user.id}"
        reverse_trade_key = f"{user.id}_{ctx.author.id}"
        if trade_key in self.active_trades or reverse_trade_key in self.active_trades:
            await ctx.send("❌ There's already an active trade between you two!\n"
                           "**Tip:** Use `/canceltrade` to cancel the existing trade.")
            return

        song_found, matches, artist = await self._resolve_trade_item(str(ctx.author.id), rarity, artist, item)
        if song_found is None:
            await ctx.send(self._ambiguous_message(rarity, item, matches, artist))
            return

        embed = discord.Embed(
            title="🔄 Trade Request",
            description=f"{ctx.author.mention} wants to trade with {user.mention}",
            color=discord.Color.blue()
        )
        embed.add_field(
            name=f"{ctx.author.display_name} offers:",
            value=self.format_song_display(str(ctx.author.id), song_found, rarity),
            inline=False
        )
        embed.add_field(
            name="Waiting for:",
            value=f"{user.mention} to offer a **{rarity}** rarity song",
            inline=False
        )
        embed.set_footer(text=f"{user.display_name}, respond with /offer to accept")

        trade_msg = await ctx.send(embed=embed)

        self.active_trades[trade_key] = {
            'initiator': ctx.author,
            'target': user,
            'initiator_song': song_found,
            'rarity': rarity,
            'message': trade_msg,
        }

        # Set timeout for trade request (2 minutes)
        await asyncio.sleep(120)
        if trade_key in self.active_trades:
            del self.active_trades[trade_key]
            embed.color = discord.Color.red()
            embed.set_footer(text="❌ Trade request expired")
            await trade_msg.edit(embed=embed)

    @commands.hybrid_command(name="gift", description="Gift a song to a user")
    @slash_only()
    @app_commands.describe(user="Who to gift it to", rarity="Rarity of the song you're gifting",
                           artist="Filter to one artist first (handy if you have many songs)",
                           item="The song to gift (default: your newest of that rarity)")
    @app_commands.autocomplete(rarity=_rarity_autocomplete, artist=_own_artist_autocomplete,
                               item=_own_item_autocomplete)
    async def gift(self, ctx, user: discord.Member, rarity: str, *,
                   artist: Optional[str] = None, item: Optional[str] = None):
        """Gift a song to a user.

        One-way and immediate -- unlike /trade there is nothing to accept and
        nothing comes back. Usage: /gift user:<who> rarity:<variant> [item]
        """
        rarity = (rarity or "").lower()
        if rarity not in RARITIES:
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

        collection_id, _song_id, _album_id, song_name, artist_name, variant, album_name = song_found[:7]
        # Read the display line before the card moves: afterwards the copy
        # number belongs to the recipient, not the giver.
        line = self.format_song_display(str(ctx.author.id), song_found, rarity)

        moved = await asyncio.to_thread(
            transfer_collection_item, collection_id, ctx.author.id, user.id)
        if not moved:
            await ctx.send("❌ That card is no longer yours — it may have just been traded away.")
            return

        embed = discord.Embed(
            title="Gift sent",
            description=f"{ctx.author.mention} gifted {user.mention} a card.",
            colour=discord.Colour(ACCENT_COLOR_INT),
        )
        embed.add_field(name="\u200b", value=line, inline=False)
        embed.set_footer(text="Gifts are one-way and cannot be undone.")
        await ctx.send(embed=embed)
        log.info("gift: %s -> %s  %s [%s]", ctx.author, user, song_name, variant)

    @gift.error
    async def gift_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Use `/gift user:<who> rarity:<variant>` — the item defaults "
                           "to your newest card of that rarity.")
            return
        if isinstance(error, (commands.MemberNotFound, commands.BadArgument)):
            await ctx.send("❌ **Invalid user!** Mention someone with @.")
            return
        await report_unhandled(log, ctx, error, command="gift")

    @commands.hybrid_command(name="offer", description="Respond to a trade request with your offer")
    @slash_only()
    @app_commands.describe(rarity="Rarity of the song you're offering",
                           artist="Filter to one artist first (handy if you have many songs)",
                           item="The song to offer (default: your newest of that rarity)")
    @app_commands.autocomplete(rarity=_rarity_autocomplete, artist=_own_artist_autocomplete, item=_own_item_autocomplete)
    async def offer(self, ctx, rarity: str, *, artist: Optional[str] = None,
                    item: Optional[str] = None):
        """Respond to a trade request with your offer.
        Usage: .offer rarity <song> — or use /offer for autocomplete (+ optional artist filter).
        """
        rarity = (rarity or "").lower()
        if rarity not in RARITIES:
            await ctx.send(f"❌ **Invalid Rarity: `{rarity}`**\n"
                           f"**Valid Rarities:** {', '.join(f'`{r}`' for r in RARITIES)}")
            return

        trade_key = None
        trade_data = None
        for key, data in self.active_trades.items():
            if data['target'].id == ctx.author.id:
                trade_key, trade_data = key, data
                break

        if not trade_data:
            await ctx.send("❌ **No active trade request found for you!**\n"
                           "**Note:** Someone needs to trade with you first using `/trade`.")
            return

        if rarity != trade_data['rarity']:
            await ctx.send(f"❌ Rarity mismatch! The trade requires **{trade_data['rarity']}** "
                           f"rarity, but you offered **{rarity}**.")
            return

        song_found, matches, artist = await self._resolve_trade_item(str(ctx.author.id), rarity, artist, item)
        if song_found is None:
            await ctx.send(self._ambiguous_message(rarity, item, matches, artist))
            return

        initiator_song = trade_data['initiator_song']
        embed = discord.Embed(
            title="🔄 Trade Confirmation",
            description=f"Trade between {trade_data['initiator'].mention} and {ctx.author.mention}",
            color=discord.Color.gold()
        )
        embed.add_field(
            name=f"{trade_data['initiator'].display_name} offers:",
            value=self.format_song_display(str(trade_data['initiator'].id), initiator_song, trade_data['rarity']),
            inline=False
        )
        embed.add_field(
            name=f"{ctx.author.display_name} offers:",
            value=self.format_song_display(str(ctx.author.id), song_found, rarity),
            inline=False
        )
        embed.set_footer(text="Both users react with ✅ to confirm the trade, or ❌ to cancel")

        await trade_data['message'].edit(embed=embed)
        await trade_data['message'].add_reaction("✅")
        await trade_data['message'].add_reaction("❌")

        trade_data['target_song'] = song_found
        trade_data['ready_for_confirmation'] = True

        def check(reaction, reactor):
            return (reactor == trade_data['initiator'] or reactor == ctx.author) and \
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
                del self.active_trades[trade_key]
                return

            # Execute the trade: swap songs between both users' collections.
            remove_from_collection(str(trade_data['initiator'].id), initiator_song[1], initiator_song[2], trade_data['rarity'])
            remove_from_collection(str(ctx.author.id), song_found[1], song_found[2], rarity)

            add_song_to_collection(str(ctx.author.id), initiator_song[1], initiator_song[2], trade_data['rarity'])
            add_song_to_collection(str(trade_data['initiator'].id), song_found[1], song_found[2], rarity)

            embed.color = discord.Color.green()
            embed.title = "✅ Trade Completed!"
            embed.clear_fields()
            embed.add_field(
                name=f"{trade_data['initiator'].display_name} received:",
                value=self.format_song_display(str(trade_data['initiator'].id), song_found, rarity),
                inline=False
            )
            embed.add_field(
                name=f"{ctx.author.display_name} received:",
                value=self.format_song_display(str(ctx.author.id), initiator_song, trade_data['rarity']),
                inline=False
            )
            embed.set_footer(text="Trade successful! Check your collections.")
            await trade_data['message'].edit(embed=embed)
            log.info("Trade completed between %s and %s", trade_data['initiator'].id, ctx.author.id)

            del self.active_trades[trade_key]

        except asyncio.TimeoutError:
            embed.color = discord.Color.red()
            embed.set_footer(text="❌ Trade confirmation timed out")
            await trade_data['message'].edit(embed=embed)
            del self.active_trades[trade_key]

    @commands.hybrid_command(name="canceltrade", description="Cancel any active trade requests")
    @slash_only()
    async def canceltrade(self, ctx):
        """Cancel active trade requests"""
        cancelled = False

        for key, data in list(self.active_trades.items()):
            if data['initiator'].id == ctx.author.id or data['target'].id == ctx.author.id:
                embed = discord.Embed(
                    title="❌ Trade Cancelled",
                    description=f"Trade between {data['initiator'].mention} and {data['target'].mention} has been cancelled.",
                    color=discord.Color.red()
                )
                await data['message'].edit(embed=embed)
                del self.active_trades[key]
                cancelled = True

        if cancelled:
            await ctx.send("⚠️ Your active trade has been cancelled.")
        else:
            await ctx.send("❌ You don't have any active trades to cancel.")

    @trade.error
    async def trade_error(self, ctx, error):
        """Handle errors for the trade command"""
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing arguments!**\n"
                           "**Usage:** `.trade @user rarity <song>` — or use `/trade` for autocomplete.")
        elif isinstance(error, commands.MemberNotFound) or isinstance(error, commands.BadArgument):
            await ctx.send("❌ **Invalid user!** Make sure to properly mention (@) a valid user.")
        else:
            raise error

    @offer.error
    async def offer_error(self, ctx, error):
        """Handle errors for the offer command"""
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ **Missing arguments!**\n"
                           "**Usage:** `.offer rarity <song>` — or use `/offer` for autocomplete.")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(TradeCog(bot))
