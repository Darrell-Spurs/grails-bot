"""Artwork for a single card, chosen by its variant.

A card's picture should look the way it looked when it was pulled. Before this,
every variant showed the plain Spotify cover in /view, so a glitched card and a
regular one were indistinguishable once the drop message scrolled away.

The effects are the same functions the pull path uses, so a card cannot look one
way in a choice drop and another in /view.

Everything here is blocking (downloads and PIL work), so callers run it through
asyncio.to_thread.
"""
import logging
from io import BytesIO

import discord
from PIL import Image

from utils.artwork import center_square, fetch_image
from utils.helpers import create_glitched_effect, create_sketch_effect
from utils.mystic import mythic_card_bytes
from utils.vinyl import vinyl_static_bytes, vinyl_static_sig_bytes

log = logging.getLogger("grails.cardart")

# Big enough to look sharp in an embed, small enough that the effects stay
# quick. The vinyl renders are square, so this is their diameter too.
ART_SIZE = 500

ART_FILENAME = "card.png"

# Variants whose picture is the plain Spotify cover.
PLAIN_VARIANTS = {"default"}


def _cover(url):
    """The album art as a square RGB image, ART_SIZE on a side."""
    return center_square(fetch_image(url)).resize((ART_SIZE, ART_SIZE), Image.LANCZOS)


def render_card_art(card):
    """A BytesIO PNG for this card, or None to fall back to the plain cover.

    Returning None rather than rendering the cover ourselves lets the embed keep
    pointing at Spotify's URL, which costs no upload and no render.
    """
    url = card.get("album_image")
    if not url:
        return None

    variant = (card.get("variant") or "default").lower()
    if variant in PLAIN_VARIANTS:
        return None

    rarity = (card.get("rarity") or "common").lower()
    try:
        if variant == "vinyl":
            return vinyl_static_bytes(url, output_size=ART_SIZE, rarity=rarity)
        if variant == "sig_vinyl":
            return vinyl_static_sig_bytes(url, card.get("artist") or "",
                                          output_size=ART_SIZE, rarity=rarity)
        if variant == "mythic":
            # Seeded on the card's own id so the glitter is identical every
            # time this card is viewed, rather than reshuffling on each open.
            # The copy number is stored on the card row (db._card_dict), so
            # it no longer needs a lookup of its own here.
            return mythic_card_bytes(url, copy_number=card.get("copy_number"),
                                     size=ART_SIZE, seed=card.get("collection_id"),
                                     rarity=rarity)

        if variant == "glitched":
            image = create_glitched_effect(_cover(url))
        elif variant == "sketch":
            image = create_sketch_effect(_cover(url))
        else:
            return None

        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        buffer.seek(0)
        return buffer
    except Exception:
        # A card that will not render should still be viewable: the embed falls
        # back to the plain cover rather than the command failing.
        log.exception("card art failed for %s (%s)", card.get("song_name"), variant)
        return None


def card_art_file(card):
    """render_card_art() wrapped as a discord.File, or None."""
    buffer = render_card_art(card)
    return discord.File(buffer, filename=ART_FILENAME) if buffer else None
