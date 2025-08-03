import requests, cv2
if __name__ == "__main__":
    from spotify_utils import get_access_token
    from mystic import main
else:
    from .spotify_utils import get_access_token
    from .mystic import main

import asyncio, sys, os, uuid, random
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance
from io import BytesIO
import numpy as np
import colorsys

# Add the project root to the Python path for db import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from db import *

async def get_filtered_albums(included, artist_name, ctx, token):
    headers = {"Authorization": f"Bearer {token}"}

    # Step 1: Search artist ID
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
        await ctx.send(f"❌ Artist '{artist_name}' not found.")
        return

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
        await ctx.send(f"💿 No albums found for **{artist_name_corrected}**.")
        return

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
    return songs, artist_name_corrected

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

    # Use the existing get_songs_from_album function to get all songs
    songs_by_album_id, artist_name_corrected = await get_songs_from_album(album_type, artist_name, ctx, token)

    if not songs_by_album_id:
        if ctx:
            await ctx.send(f"❌ No songs found for **{artist_name}**.")
        return 0, artist_name

    # Get album information for database operations
    filtered_albums, _, _ = await get_filtered_albums(album_type, artist_name, ctx, token)

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

                # Add song to songs table (with default rarity "common")
                existing_song_id = add_song(song_id, song_name, artist_name_corrected, "common")

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

def get_random_song():
    artists = get_all_artists()
    random_artist = random.choice(artists)
    songs = get_song_by_artist(random_artist)
    random_song = random.choice(songs) if songs else None
    print(f"{random_song}, {random_artist}")
    return random_song[0], random_song[1], random_artist

def get_random_album(song_id):
    albums = get_album_details_by_song(song_id)
    album = random.choice(albums)
    return album

def draw_variant():
    variants = ["default", "glitched", "sketch", "mythic", "diamond"]
    random_num = random.random()
    mythic_chance = 0.005 / 3
    sketch_chance = 0.03 / 3
    glitched_chance = 0.12 / 3

    if random_num < mythic_chance:
        return "mythic"
    if random_num < sketch_chance + mythic_chance:  # 2% chance to be sketch
        return "sketch"
    if random_num < glitched_chance + sketch_chance + mythic_chance:  # 10% chance to be glitched:
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

def create_mystic_effect(url):
    mystic_img = main(url)
    return mystic_img

def make_3_song_collage(image_urls, titles, artists, variants, output_path="collage.jpg"):
    SCALE = 2

    images = []
    for i in range(len(image_urls)):
        url = image_urls[i]
        response = requests.get(url)
        img = Image.open(BytesIO(response.content)).convert("RGB").resize((300 * SCALE, 300 * SCALE))
        if variants[i] == "glitched":
            img = create_glitched_effect(img)
        elif variants[i] == "sketch":
            img = create_sketch_effect(img)
        images.append(img)

    try:
        font_title = ImageFont.truetype("calibrib.ttf", 22 * SCALE)
        font_artist = ImageFont.truetype("calibri.ttf", 18 * SCALE)
    except:
        font_title = ImageFont.load_default()
        font_artist = ImageFont.load_default()

    spacing = 20 * SCALE
    img_width = images[0].width
    img_height = images[0].height
    text_height = 60 * SCALE
    total_width = img_width * 3 + spacing * 2
    total_height = img_height + text_height

    canvas = Image.new("RGB", (total_width, total_height), "#1a1a1e")
    draw = ImageDraw.Draw(canvas)

    for i, img in enumerate(images):
        x = i * (img_width + spacing)
        canvas.paste(img, (x, 0))

        title_text = titles[i]
        artist_text = artists[i]

        if len(title_text) > 25:
            title_text = title_text[:25] + "..."

        title_w = draw.textlength(title_text, font=font_title)
        artist_w = draw.textlength(artist_text, font=font_artist)
        title_x = x + (img_width - title_w) // 2
        artist_x = x + (img_width - artist_w) // 2
        draw.text((title_x, img_height + 5 * SCALE), title_text, font=font_title, fill="#eeeeee")
        draw.text((artist_x, img_height + 30 * SCALE), artist_text, font=font_artist, fill="#cccccc")

    final_image = canvas.resize((canvas.width, canvas.height), Image.Resampling.LANCZOS)
    final_image.save(output_path)

    # print(f"✅ Saved collage to {output_path}")


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
    "Nancy Ajram"
]    

async def main():
    for artist_name in ARTISTS_TO_ADD:
        token = get_access_token()
        print(f"\n--- Processing {artist_name} ---")

        album_type = "album"  # or "single" based on your needs
        # Save albums to database
        albums_saved, corrected_name = await save_albums_to_db(album_type, artist_name, token=token)
        print(f"Albums saved: {albums_saved}")

        # Save songs to database
        songs_saved, corrected_name = await add_songs_to_db(album_type, artist_name, token=token)
        print(f"Songs saved: {songs_saved}")

        print(f"Finished processing {corrected_name}\n")

if __name__ == "__main__":
    # Example usage
    asyncio.run(main())
    # get_random_song()
