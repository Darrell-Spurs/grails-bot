"""Artist autocompletes shared by several slash commands.

Two different questions, each of which used to be answered by two copies of the
same function in different cogs:

  * catalog_artist_autocomplete -- every artist in the pool (/albums, /songs,
    /mythiccheck).
  * owned_artist_autocomplete -- only artists in one player's collection
    (/collection, /view), since offering the whole catalogue there would mostly
    suggest filters that match nothing.
"""
import asyncio
import logging

import discord
from discord import app_commands

import db

log = logging.getLogger("grails.autocomplete")

# Discord rejects the entire response if a single choice's name or value is
# longer than this, so every choice is clipped rather than trusted.
CHOICE_LIMIT = 100
MAX_CHOICES = 25


def _choices(names, current):
    cur = (current or "").lower()
    return [app_commands.Choice(name=n[:CHOICE_LIMIT], value=n[:CHOICE_LIMIT])
            for n in names if cur in n.lower()][:MAX_CHOICES]


async def catalog_artist_autocomplete(interaction: discord.Interaction, current: str):
    """Every artist in the pool. Served from the catalogue cache, so a keystroke
    costs no query."""
    try:
        artists = await asyncio.to_thread(db.get_all_artists)
    except Exception:
        log.exception("catalog artist autocomplete failed")
        return []
    return _choices(artists, current)


async def owned_artist_autocomplete(interaction: discord.Interaction, current: str):
    """Artists the viewed collection contains: the `user` option's, when one has
    been picked, otherwise the invoker's own."""
    target = getattr(interaction.namespace, "user", None)
    owner_id = target.id if target else interaction.user.id
    try:
        artists = await asyncio.to_thread(db.get_user_collection_artists, owner_id)
    except Exception:
        log.exception("owned artist autocomplete failed")
        return []
    return _choices(artists, current)
