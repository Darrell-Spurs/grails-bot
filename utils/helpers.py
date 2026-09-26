import cv2

import sys, os, random
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageChops
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import numpy as np

# Add the project root to the Python path for db import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from db import *
from utils import odds
from utils import aesthetics

import logging
from utils.artwork import center_square, fetch_image
log = logging.getLogger("grails.helpers")

# Sour Patch Kids fallback item (used when no real song can be found)
SOURPATCH_ID = "__sourpatchkids__"
SOURPATCH_NAME = "Sour Patch Kids"
SOURPATCH_ARTIST = "Sour Patch Kids"
SOURPATCH_IMAGE = os.path.join(os.path.dirname(__file__), "images", "sourpatchkids.png")

# Asset directories resolved from this file, not the working directory, so assets
# still load when the process is started from somewhere else (as hosts often do).
_UTILS_DIR = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(_UTILS_DIR, "fonts")
IMAGES_DIR = os.path.join(_UTILS_DIR, "images")

# helper functions


def _sourpatch_pick():
    return {
        "song_id": SOURPATCH_ID, "song_name": SOURPATCH_NAME, "artist": SOURPATCH_ARTIST,
        "rarity": "basic", "album_id": SOURPATCH_ID, "album_name": SOURPATCH_NAME,
        "album_image": SOURPATCH_IMAGE,
    }


def get_random_songs(count=3, variant="", overdraw=3):
    """Draw `count` songs (with album and rarity) in a single database query.

    Same distribution as calling get_random_song() `count` times: an
    (artist, rarity) pair is drawn with the usual weights, and pairs that turn
    out to have no songs are skipped -- exactly what the old reroll loop did,
    just resolved in memory instead of one network round-trip per attempt.

    `overdraw` is how many extra pairs to ask about so an empty pair almost
    never costs a second query.

    Returns a list of dicts: song_id, song_name, artist, rarity, album_id,
    album_name, album_image.
    """
    artists = get_all_artists()
    if not artists:
        return [_sourpatch_pick() for _ in range(count)]

    rates = odds.RARITY_RATES.get(variant, odds.RARITY_RATES[""])
    names = list(rates.keys())
    weights = list(rates.values())

    pairs = [(random.choice(artists), random.choices(names, weights=weights)[0])
             for _ in range(count * max(overdraw, 1))]
    buckets = get_random_song_candidates(pairs)

    picked = []
    for artist, rarity in pairs:
        if len(picked) == count:
            break
        rows = buckets.get((artist.lower(), rarity.lower()))
        if rows:
            picked.append(random.choice(rows))

    # Only reached when the over-drawn pairs were all empty, which needs a very
    # sparse catalog; the old code fell back to Sour Patch Kids here too.
    while len(picked) < count:
        picked.append(_sourpatch_pick())
    return picked


def get_random_song(variant = ""):
    """Draw one song. get_random_songs() is the batched form and the one the
    pull path uses; this remains for callers that need a single pick."""
    artists = get_all_artists()
    rarity_rate = odds.RARITY_RATES.get(variant, odds.RARITY_RATES[""])
    rarities = list(rarity_rate.keys())
    weights = list(rarity_rate.values())

    # Reroll (artist, rarity) until we find a real song. A random (artist, rarity)
    # pair can legitimately be empty, which previously crashed on random_song[None].
    for _ in range(25):
        random_artist = random.choice(artists)
        random_rarity = random.choices(rarities, weights=weights)[0]
        songs = get_songs_by_artist_and_rarity(random_artist, random_rarity)
        if songs:
            random_song = random.choice(songs)
            return random_song[1], random_song[0], random_artist

    # Nothing found (e.g. an (almost) empty catalog) -> Sour Patch Kids fallback.
    log.warning("get_random_song: no song after retries; falling back to Sour Patch Kids")
    return SOURPATCH_NAME, SOURPATCH_ID, SOURPATCH_ARTIST

def get_random_album(song_id):
    if song_id == SOURPATCH_ID:
        return (SOURPATCH_ID, SOURPATCH_NAME, SOURPATCH_IMAGE)
    albums = get_album_details_by_song(song_id)
    if not albums:
        return (SOURPATCH_ID, SOURPATCH_NAME, SOURPATCH_IMAGE)
    return random.choice(albums)

def pick_unique_song(user_id, dedup_variant, weight_variant="", max_attempts=50):
    """Pick a random song (same distribution as get_random_song) that the user does
    not already own under `dedup_variant`. Runs entirely synchronously so callers can
    offload the whole thing with asyncio.to_thread in one hop.

    Returns (song_name, song_id, artist, album, rarity).
    """
    song, song_id, artist = get_random_song(variant=weight_variant)
    album = get_random_album(song_id)
    for _ in range(max_attempts):
        if get_user_song_by_details(str(user_id), dedup_variant, song, artist, album[1]):
            song, song_id, artist = get_random_song(variant=weight_variant)
            album = get_random_album(song_id)
        else:
            break
    return song, song_id, artist, album, get_song_rarity(song_id)

def draw_variant():
    """Roll one song's variant.

    The tunables in utils/odds.py are stated per *pull*; each is converted to
    the per-song probability that produces it across the three songs a pull
    offers, then the roll walks the cumulative bands.
    """
    random_num = random.random()

    mythic_chance = odds.per_song_chance(odds.MYTHIC_PER_PULL)
    gutscookie_chance = odds.per_song_chance(odds.GUTSCOOKIE_PER_PULL)
    sketch_chance = odds.per_song_chance(odds.SKETCH_PER_PULL)
    glitched_chance = odds.per_song_chance(odds.GLITCHED_PER_PULL)

    if random_num < mythic_chance:
        return "mythic"
    if random_num < mythic_chance + gutscookie_chance:
        return "gutscookie"
    if random_num < sketch_chance + mythic_chance + gutscookie_chance:
        return "sketch"
    if random_num < glitched_chance + sketch_chance + mythic_chance + gutscookie_chance:
        return "glitched"
    return "default"

def create_glitched_effect(image):
    img = image.convert("RGB")
    width, height = img.size

    r, g, b = img.split()
    result = Image.merge("RGB", (
        ImageChops.offset(r, round(width * 7 / 450), 0),
        g,
        ImageChops.offset(b, -round(width * 6 / 450), 0),
    ))

    # Position, height, and sideways shift of each glitch band
    bands = [
        (174, 19, 8), (205, 10, -7), (258, 22, 17),
        (281, 14, -10), (347, 12, 8), (402, 9, -7),
    ]

    for y, band_height, displacement in bands:
        top = round(y * height / 450)
        bottom = min(height, top + max(1, round(band_height * height / 450)))
        if top >= height:
            continue

        band = result.crop((0, top, width, bottom))
        band = ImageChops.offset(
            band, round(displacement * width / 450), 0
        )
        result.paste(band, (0, top))

    return ImageEnhance.Color(result).enhance(1.15)

def create_sketch_effect(img_input):
    img = cv2.cvtColor(np.array(img_input), cv2.COLOR_RGB2BGR)
    gray_image = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Invert and blur
    inverted = 255 - gray_image
    blurred = cv2.GaussianBlur(inverted, (15, 15), sigmaX=0, sigmaY=0)
    inverted_blur = 255 - blurred

    # Final sketch
    sketch = cv2.divide(gray_image, inverted_blur, scale=256.0)

    # Enhance contrast and sharpness using PIL
    pil_img = Image.fromarray(sketch)
    pil_img = ImageEnhance.Contrast(pil_img).enhance(3)   # More fill and pop
    pil_img = ImageEnhance.Sharpness(pil_img).enhance(3)  # Clearer edges
    pil_ima = ImageEnhance.Contrast(pil_img).enhance(1.5)  # Slightly more contrast for pop
    # fixed_size = (300, 300)
    # pil_img = pil_img.resize(fixed_size, Image.ANTIALIAS)

    return pil_img

# Fonts were re-opened from disk on every collage (4-6 TrueType loads per
# pull). They never change, so load each face once.
_font_cache = {}


def _font(filename, size):
    key = (filename, size)
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(os.path.join(FONTS_DIR, filename), size)
        except OSError:
            return None
    return _font_cache[key]


COVER_TIMEOUT = 10  # seconds; without one a stalled CDN pins the worker thread


def _load_cover(url, size):
    """Fetch one album cover and resize it to size x size. Local paths (the Sour
    Patch fallback) are opened directly."""
    return fetch_image(url, timeout=COVER_TIMEOUT).resize((size, size))


def make_3_song_collage(image_urls, titles, artists, variants, rarities, output_path=None, return_bytes=False):
    SCALE = 2

    # The three covers are independent network fetches, so they go out at once
    # rather than one after another (measured ~383ms sequential vs ~131ms).
    side = 300 * SCALE
    with ThreadPoolExecutor(max_workers=3) as pool:
        images = list(pool.map(lambda u: _load_cover(u, side), image_urls))

    # Effects stay on this thread: they are CPU-bound, so a pool would only add
    # contention.
    for i, variant in enumerate(variants):
        if variant == "glitched":
            images[i] = create_glitched_effect(images[i])
        elif variant == "sketch":
            images[i] = create_sketch_effect(images[i])

    font_title = _font("calibrib.ttf", 22 * SCALE) or ImageFont.load_default()
    font_artist = _font("calibri.ttf", 18 * SCALE) or ImageFont.load_default()
    rarity_font = _font("galada.ttf", 20 * SCALE) or _font("segeo-script.ttf", 24 * SCALE)

    spacing = 20 * SCALE
    img_width = images[0].width
    img_height = images[0].height
    text_height = 80 * SCALE  # Increased to accommodate rarity rectangles
    total_width = img_width * 3 + spacing * 2
    total_height = img_height + text_height

    canvas = Image.new("RGB", (total_width, total_height), "#1a1a1e")
    draw = ImageDraw.Draw(canvas)

    for i, img in enumerate(images):
        x = i * (img_width + spacing)
        canvas.paste(img, (x, 0))

        title_text = titles[i]
        artist_text = artists[i]
        rarity_text = (rarities[i] or "basic").title()
        
        rarity_color = aesthetics.rarity_hex(rarity_text)

        if len(title_text) > 25:
            title_text = title_text[:25] + "..."

        title_w = draw.textlength(title_text, font=font_title)
        artist_w = draw.textlength(artist_text, font=font_artist)
        title_x = x + (img_width - title_w) // 2
        artist_x = x + (img_width - artist_w) // 2
        draw.text((title_x, img_height + 5 * SCALE), title_text, font=font_title, fill="#eeeeee")
        draw.text((artist_x, img_height + 30 * SCALE), artist_text, font=font_artist, fill="#cccccc")
        
        # Autograph-style rarity text below the artist name.
        rarity_text_w = draw.textlength(rarity_text, font=rarity_font)
        rarity_text_x = x + (img_width - rarity_text_w) // 2
        rarity_text_y = img_height + 48 * SCALE  # Position below artist text
        
        # Draw rarity text with color and slight shadow for depth
        shadow_offset = 1 * SCALE
        draw.text((rarity_text_x + shadow_offset, rarity_text_y + shadow_offset), rarity_text, font=rarity_font, fill="#000000")  # Shadow
        draw.text((rarity_text_x, rarity_text_y), rarity_text, font=rarity_font, fill=rarity_color)  # Main text

    final_image = canvas

    buffer = None
    if return_bytes:
        buffer = BytesIO()
        final_image.save(buffer, format="JPEG")
        buffer.seek(0)
    if output_path:
        final_image.save(output_path)
    return buffer


# ---- Song Battle helpers ----

BATTLE_CELL_SIZE = 440  # px; square thumbnail/cell size used by make_battle_collage

def load_square_thumbnail(url_or_path, size=BATTLE_CELL_SIZE):
    """Download (or open a local file) and return a square-cropped, resized RGB thumbnail.
    Used to pre-fetch and cache per-slot artwork so the battle canvas can be redrawn
    after each reveal without re-downloading already-revealed images."""
    return center_square(fetch_image(url_or_path)).resize((size, size), Image.LANCZOS)

def make_battle_collage(players, output_path=None, return_bytes=False):
    """
    Render the Song Battle canvas: one cell per player, in play order.

    players: list of dicts, one per slot:
        {
          "name": str,                    # discord display name
          "thumb": PIL.Image | None,      # None => not revealed yet (blank placeholder)
          "song": str | None,
          "artist": str | None,
          "rarity": str | None,           # one of the 5 rarities, or None/unknown
        }
    """
    SCALE = 2
    cell = BATTLE_CELL_SIZE
    spacing = 16 * SCALE
    text_height = 100 * SCALE
    n = max(1, len(players))
    total_width = cell * n + spacing * (n - 1)
    total_height = cell + text_height

    canvas = Image.new("RGB", (total_width, total_height), "#1a1a1e")
    draw = ImageDraw.Draw(canvas)

    try:
        font_name = ImageFont.truetype(os.path.join(FONTS_DIR, "calibrib.ttf"), 18 * SCALE)
        font_song = ImageFont.truetype(os.path.join(FONTS_DIR, "calibrib.ttf"), 16 * SCALE)
        font_small = ImageFont.truetype(os.path.join(FONTS_DIR, "calibri.ttf"), 14 * SCALE)
        font_qmark = ImageFont.truetype(os.path.join(FONTS_DIR, "calibrib.ttf"), 64 * SCALE)
    except Exception:
        font_name = font_song = font_small = font_qmark = ImageFont.load_default()

    # Same autograph-style script font (+ shadow) used for rarity text in .c's collage.
    try:
        font_rarity = ImageFont.truetype(os.path.join(FONTS_DIR, "galada.ttf"), 18 * SCALE)
    except Exception:
        try:
            font_rarity = ImageFont.truetype(os.path.join(FONTS_DIR, "segeo-script.ttf"), 20 * SCALE)
        except Exception:
            font_rarity = font_small


    for i, p in enumerate(players):
        x = i * (cell + spacing)

        thumb = p.get("thumb")
        if thumb is not None:
            if thumb.size != (cell, cell):
                thumb = thumb.resize((cell, cell), Image.LANCZOS)
            canvas.paste(thumb, (x, 0))
        else:
            draw.rectangle([x, 0, x + cell, cell], fill="#26262b")
            qw = draw.textlength("?", font=font_qmark)
            draw.text((x + (cell - qw) // 2, cell // 2 - 40 * SCALE), "?", font=font_qmark, fill="#55555c")

        name_text = p["name"]
        if len(name_text) > 18:
            name_text = name_text[:18] + "…"
        name_w = draw.textlength(name_text, font=font_name)
        draw.text((x + (cell - name_w) // 2, cell + 6 * SCALE), name_text, font=font_name, fill="#eeeeee")

        if p.get("song"):
            song_text = p["song"]
            if len(song_text) > 22:
                song_text = song_text[:22] + "…"
            song_w = draw.textlength(song_text, font=font_song)
            draw.text((x + (cell - song_w) // 2, cell + 32 * SCALE), song_text, font=font_song, fill="#ffffff")

            artist_text = p.get("artist") or ""
            if len(artist_text) > 26:
                artist_text = artist_text[:26] + "…"
            artist_w = draw.textlength(artist_text, font=font_small)
            draw.text((x + (cell - artist_w) // 2, cell + 56 * SCALE), artist_text, font=font_small, fill="#cccccc")

            rarity_key = p.get("rarity")
            if rarity_key:
                rarity_text = rarity_key.title()
                rarity_color = aesthetics.rarity_hex(rarity_key)
                rw = draw.textlength(rarity_text, font=font_rarity)
                rx = x + (cell - rw) // 2
                ry = cell + 76 * SCALE
                shadow_offset = 1 * SCALE
                draw.text((rx + shadow_offset, ry + shadow_offset), rarity_text, font=font_rarity, fill="#000000")
                draw.text((rx, ry), rarity_text, font=font_rarity, fill=rarity_color)
        else:
            waiting_text = "Waiting..."
            ww = draw.textlength(waiting_text, font=font_small)
            draw.text((x + (cell - ww) // 2, cell + 40 * SCALE), waiting_text, font=font_small, fill="#888888")

    buffer = None
    if return_bytes:
        buffer = BytesIO()
        canvas.save(buffer, format="PNG")
        buffer.seek(0)
    if output_path:
        canvas.save(output_path)
    return buffer


if __name__ == "__main__":
    # Example usage
    # asyncio.run(main())
    # get_random_song()
    pass
