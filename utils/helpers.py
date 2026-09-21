import requests, cv2
if __name__ == "__main__":
    from spotify_utils import get_access_token
else:
    from .spotify_utils import get_access_token

import asyncio, sys, os, uuid, random, time
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import numpy as np
import colorsys

# Add the project root to the Python path for db import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from db import *
from utils import odds
from utils.aesthetics import UNASSIGNED
from utils import aesthetics

import logging
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

# db functions

async def get_filtered_albums(included, artist_name, ctx, token):
    headers = {"Authorization": f"Bearer {token}"}
    
    artist_id = ""
    artist_name_corrected = ""
    
    # Step 1: Search artist ID
    if "||" in artist_name:
        artist_name, artist_id = map(str.strip, artist_name.split("||", 1))
        print(artist_name, artist_id)
    else:
        search_url = "https://api.spotify.com/v1/search"
        params = {
            "q": artist_name,
            "type": "artist",
            "limit": 1,
            "market": "US"
        }
        res = requests.get(search_url, headers=headers, params=params).json()
        items = res.get("artists", {}).get("items", [])

        if not items:
            if ctx:
                await ctx.send(f"❌ Artist '{artist_name}' not found.")
            return None, artist_name, ""

        artist_id = items[0]["id"]
        artist_name_corrected = items[0]["name"]

    # Step 2: Get artist albums
    albums_url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"

    all_albums = []

    while True:
        params = {
            "include_groups": included,
            "market": "US",
            "limit": 50,
            "offset": len(all_albums)
        }

        res = requests.get(albums_url, headers=headers, params=params).json()
        albums = res.get("items", [])
        all_albums.extend(albums)

        if len(albums) < 50:
            break

    # Step 3: Send the album names
    if not all_albums:
        if ctx:
            await ctx.send(f"{aesthetics.named_emoji('vinyl')} No albums found for "
                           f"**{artist_name_corrected}**.")
        return [], artist_name_corrected, artist_id

    filtered_words = ["live", "mix", "karaoke", "playlist"]
    filtered_albums = [
        album for album in all_albums
        if not any(word in album["name"].lower() for word in filtered_words)
    ]
    
    filtered_albums.sort(key=lambda album: -len(album["artists"]))
    filtered_albums.reverse()
    print(len(filtered_albums), "albums after filtering")

    return filtered_albums, artist_name_corrected, artist_id

async def get_songs_from_album(album_type, artist_name, ctx, token):
    headers = {"Authorization": f"Bearer {token}"}

    filtered_albums, artist_name_corrected, _ = await get_filtered_albums(album_type, artist_name, ctx, token)
    songs = dict()

    if not filtered_albums:
        # Return the (empty) album list too so callers don't re-fetch from Spotify.
        return songs, artist_name_corrected, filtered_albums or []

    for album in filtered_albums:
        album_id = album["id"]
        tracks_url = f"https://api.spotify.com/v1/albums/{album_id}/tracks"

        # Get album name
        album_name = album["name"]

        # Get all tracks (handle pagination just in case)
        all_tracks = []
        offset = 0
        while True:
            params = {
                "limit": 50,
                "offset": offset
            }
            res = requests.get(tracks_url, headers=headers, params=params).json()
            items = res.get("items", [])
            all_tracks.extend(items)

            if len(items) < 50:
                break
            offset += 50

        # Format: song name - album name
        filtered_tracks = [
            track for track in all_tracks
            if any(artist["name"] == artist_name_corrected for artist in track["artists"])
        ]

        formatted_tracks = [track for track in filtered_tracks]
        songs[album_id] = formatted_tracks
    return songs, artist_name_corrected, filtered_albums

async def save_albums_to_db(album_type, artist_name, ctx=None, token=None):
    """
    Save all albums from an artist to the database
    
    Args:
        artist_name (str): Name of the artist
        ctx: Discord context (optional, for sending messages)
        token (str): Spotify access token (optional, will get new one if not provided)
    
    Returns:
        tuple: (number_of_albums_saved, artist_name_corrected)
    """
    if not token:
        token = get_access_token()
    
    # Get all albums for the artist
    filtered_albums, artist_name_corrected, artist_id = await get_filtered_albums(album_type, artist_name, ctx, token)

    if not filtered_albums:
        if ctx:
            await ctx.send(f"❌ No albums found for **{artist_name}**.")
        return 0, artist_name
    
    albums_saved = 0
    
    # Save each album to database
    for album in filtered_albums:
        try:
            album_id = album["id"]
            album_name = album["name"]
            album_image = album.get("images", [{}])[0].get("url", None) 
            
            # Save album to database
            add_album(album_id, album_name, artist_name_corrected, artist_id, album_image)
            albums_saved += 1
            
        except Exception as e:
            print(f"Error saving album {album.get('name', 'Unknown')}: {e}")
            continue
    
    if ctx:
        await ctx.send(f"✅ Saved **{albums_saved}** albums by **{artist_name_corrected}** to database.")

    invalidate_artists_cache()  # a new artist may now be pullable
    return albums_saved, artist_name_corrected

async def add_songs_to_db(album_type, artist_name, ctx=None, token=None):
    """
    Save all songs from an artist's albums to the database
    
    Args:
        artist_name (str): Name of the artist
        ctx: Discord context (optional, for sending messages)
        token (str): Spotify access token (optional, will get new one if not provided)
    
    Returns:
        tuple: (number_of_songs_saved, artist_name_corrected)
    """
    if not token:
        token = get_access_token()
    
    # Use the existing get_songs_from_album function to get all songs.
    # It also returns the album metadata it already fetched, so we don't page
    # the Spotify albums endpoint a second time.
    songs_by_album_id, artist_name_corrected, filtered_albums = await get_songs_from_album(album_type, artist_name, ctx, token)

    if not songs_by_album_id:
        if ctx:
            await ctx.send(f"❌ No songs found for **{artist_name}**.")
        return 0, artist_name

    filtered_albums = filtered_albums or []

    songs_saved = 0
    
    # Process each album and its songs
    for album_id, tracks in songs_by_album_id.items():
        # Find the album info from filtered_albums
        album_info = next((album for album in filtered_albums if album["id"] == album_id), None)
        
        if not album_info:
            print(f"Warning: Could not find album info for ID '{album_id}'")
            continue
            
        album_name = album_info["name"]
        
        # Ensure the album exists in the database
        # try:
        #     add_album(album_id, album_name, artist_name_corrected)
        # except Exception as e:
        #     print(f"Error saving album {album_name}: {e}")
        #     continue
        
        # Save each song and link it to the album (tracks already contain full track objects)
        for track in tracks:
            try:
                song_id = uuid.uuid4().hex[:8]
                spotify_song_id = track["id"]
                song_name = track["name"]
                
                # Imported songs start unassigned: an admin sets the real
                # tier with .sr, and the draw tables name only real tiers,
                # so nothing unassigned can be pulled in the meantime.
                existing_song_id = add_song(song_id, song_name, artist_name_corrected, UNASSIGNED)

                if existing_song_id is not None:
                    song_id = existing_song_id

                # Link song to album in song_album table
                link_song_album(song_id, spotify_song_id, album_id, album_name)
                
                songs_saved += 1
                
            except Exception as e:
                print(f"Error saving song {track.get('name', 'Unknown')}: {e}")
                continue
    
    if ctx:
        await ctx.send(f"✅ Saved **{songs_saved}** songs by **{artist_name_corrected}** to database.")
    
    return songs_saved, artist_name_corrected



# helper functions

# ---- short-TTL cache for the (slowly changing) artist list ----
# get_all_artists() was queried on every single pull (~3.4ms each).
_artists_cache = {"value": None, "expires_at": 0.0}
_ARTISTS_TTL = 300  # seconds

def _cached_artists():
    now = time.time()
    if _artists_cache["value"] is None or now >= _artists_cache["expires_at"]:
        _artists_cache["value"] = get_all_artists()
        _artists_cache["expires_at"] = now + _ARTISTS_TTL
    return _artists_cache["value"]

def cached_artists():
    """The artist-name list, cached and invalidated when one is added.

    Public counterpart to _cached_artists, for callers outside this module --
    notably the catalog autocompletes, which resolve an artist name on every
    keystroke and must not hit the database each time.
    """
    return _cached_artists()


def invalidate_artists_cache():
    """Call after adding/removing artists so pulls see them immediately."""
    _artists_cache["value"] = None
    _artists_cache["expires_at"] = 0.0

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
    artists = _cached_artists()
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
    artists = _cached_artists()
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

def create_glitch_effect_old(image):
    r, g, b = image.split()

    # Slightly shift each color channel
    r_np = np.array(r)
    g_np = np.roll(np.array(g), 5, axis=0)   # vertical shift
    b_np = np.roll(np.array(b), -5, axis=1)  # horizontal shift

    glitched = Image.merge("RGB", (Image.fromarray(r_np), Image.fromarray(g_np), Image.fromarray(b_np)))

    # Add horizontal slice glitching
    for i in range(0, glitched.height, 20):
        shift = np.random.randint(-10, 10)
        box = (0, i, glitched.width, i + 10)
        region = glitched.crop(box)
        glitched.paste(region, (shift, i))

    return glitched

def create_glitched_effect(image):
    shift = -0.1
    img = image.convert("RGB")
    arr = np.array(img) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]

    # Convert to HSV
    h, s, v = np.vectorize(colorsys.rgb_to_hsv)(r, g, b)

    # Shift hue
    h = (h + shift) % 1.0

    # Convert back to RGB
    r, g, b = np.vectorize(colorsys.hsv_to_rgb)(h, s, v)
    shifted_arr = np.stack([r, g, b], axis=-1) * 255
    return Image.fromarray(shifted_arr.astype("uint8"))

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
    """Fetch one album cover and square it off. Local paths (the Sour Patch
    fallback) are opened directly."""
    if url and os.path.exists(url):
        img = Image.open(url).convert("RGB")
    else:
        response = requests.get(url, timeout=COVER_TIMEOUT)
        response.raise_for_status()
        img = Image.open(BytesIO(response.content)).convert("RGB")
    return img.resize((size, size))


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
    if url_or_path and os.path.exists(url_or_path):
        img = Image.open(url_or_path).convert("RGB")
    else:
        response = requests.get(url_or_path, timeout=15)
        img = Image.open(BytesIO(response.content)).convert("RGB")
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    return img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)

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


ARTISTS_TO_ADD = [
    # "Olivia Rodrigo",
    # "Taylor Swift",
    # "Billie Eilish",
    # "The Weeknd",
    # "Dua Lipa",
    # "Gracie Abrams",
    # "Tate McRae",
    # "Sabrina Carpenter",
    # "Conan Gray",
    # "ROSÉ",
    # "Nancy Ajram"

]    

async def add_artist_to_db(artist_name, album_type):
    token = get_access_token()
    print(f"\n--- Processing {artist_name} ---")

    # Save albums to database
    albums_saved, corrected_name = await save_albums_to_db(album_type, artist_name, token=token)
    print(f"Albums saved: {albums_saved}")
    
    # Save songs to database
    songs_saved, corrected_name = await add_songs_to_db(album_type, artist_name, token=token)
    print(f"Songs saved: {songs_saved}")
    
    print(f"Finished processing {corrected_name}\n")
    
    return {
        "artist_name": corrected_name,
        "albums_saved": albums_saved,
        "songs_saved": songs_saved
    }

if __name__ == "__main__":
    # Example usage
    # asyncio.run(main())
    # get_random_song()
    pass
