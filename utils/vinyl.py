# pip install pillow requests numpy
import io
import math
import os
import unicodedata
from urllib import response
import requests
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
from io import BytesIO
from utils import aesthetics

# Resolved from this file rather than the working directory, so signature art
# still loads when the process is started from a different cwd.
SIGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images", "sigs")

SCALE = 2
def signature_filename(artist: str) -> str:
    """The file `artist`'s signature vinyl looks for."""
    return f"{artist.replace(' ', '_')}_sig.png"


def find_signature(artist: str):
    """Locate an artist's signature file, or None.

    Exact match first, which is the normal case. The fallbacks matter because
    the names carry accents: "ROSE" and "Renee Rapp" can be stored either as a
    precomposed character (NFC, what Windows writes) or as a letter plus a
    combining accent (NFD, what macOS writes). The two are different byte
    strings, so a signature uploaded from a Mac would silently never be found.
    Comparing normalised forms makes the lookup agnostic to which was used.

    The final pass is case-insensitive, so "the weeknd_sig.png" still resolves.
    """
    wanted = signature_filename(artist)
    direct = os.path.join(SIGS_DIR, wanted)
    if os.path.exists(direct):
        return direct
    if not os.path.isdir(SIGS_DIR):
        return None

    target = unicodedata.normalize("NFC", wanted)
    entries = os.listdir(SIGS_DIR)
    for name in entries:
        if unicodedata.normalize("NFC", name) == target:
            return os.path.join(SIGS_DIR, name)
    lowered = target.lower()
    for name in entries:
        if unicodedata.normalize("NFC", name).lower() == lowered:
            return os.path.join(SIGS_DIR, name)
    return None


def signature_files():
    """Every signature file present, as a sorted list of bare filenames."""
    if not os.path.isdir(SIGS_DIR):
        return []
    return sorted(f for f in os.listdir(SIGS_DIR) if f.lower().endswith("_sig.png"))


def _download_image(url: str, timeout: int = 15) -> Image.Image:
    response = requests.get(url)
    img = Image.open(BytesIO(response.content)).convert("RGB").resize((300 * SCALE, 300 * SCALE))
    return img

def _center_crop_square(img: Image.Image) -> Image.Image:
    """Crop the longest dimension to make the image square."""
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))

def _circle_mask(size: int, radius: int, blur: float = 1.5) -> Image.Image:
    """Create a soft-edged circular alpha mask."""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    cx = cy = size // 2
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=255)
    if blur > 0:
        m = m.filter(ImageFilter.GaussianBlur(blur))
    return m

def _vinyl_groove_texture(size: int, record_radius: int,
                          groove_spacing_px: float = 12.0,
                          groove_strength: float = 100.0,
                          vignette_power: float = 1.4) -> Image.Image:
    """
    Create a grayscale texture with concentric grooves + gentle vignette.
    White = highlight, dark = shadow. Output is mode 'L'.
    """
    h = w = size
    y, x = np.ogrid[-h/2:h/2, -w/2:w/2]
    r = np.sqrt(x*x + y*y)
    r_norm = np.clip(r / record_radius, 0.0, 1.0)

    # Grooves: sinusoidal rings in radial space
    # spacing controls how many pixels per groove - increased for bigger gaps
    grooves = 0.5 + 0.5 * np.sin(2 * math.pi * (r / groove_spacing_px))

    # Base brightness (near-black) + grooves + vignette falloff
    # Increased contrast for more obvious grooves
    base = 15.0
    groove_term = groove_strength * (grooves ** 1.0)  # Less power for sharper contrast
    vignette = 55.0 * (r_norm ** vignette_power)
    gray = np.clip(base + groove_term - vignette, 3, 110).astype(np.uint8)

    tex = Image.fromarray(gray, mode="L")
    # Reduced blur to keep grooves sharp and obvious
    return tex.filter(ImageFilter.GaussianBlur(0.2))

def _specular_highlight(size: int, record_radius: int) -> Image.Image:
    """
    Create a soft diagonal specular highlight overlay (RGBA).
    """
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    cx = cy = size // 2
    # Draw a tilted ellipse near the top-left as the light streak
    w = int(record_radius * 1.2)
    h = int(record_radius * 0.5)
    bbox = (cx - w//2 - int(0.05 * record_radius),
            cy - h//2 - int(0.65 * record_radius),
            cx + w//2 - int(0.05 * record_radius),
            cy + h//2 - int(0.65 * record_radius))
    d.ellipse(bbox, fill=255)
    mask = mask.rotate(-22, resample=Image.BICUBIC).filter(ImageFilter.GaussianBlur(16))

    # Clip the streak to the record. The 16px blur pushes it past the rim at an
    # alpha of about 9 -- invisible while the frame is still RGBA, but GIF has
    # no partial transparency, so on export those faint pixels resolve to opaque
    # near-black and print a dark smear on the background outside the disc.
    # Reusing the disc's own soft-edged mask makes the highlight fade at the rim
    # exactly as the record does, and because both are centred circles the clip
    # survives the per-frame rotation about that same centre.
    mask = ImageChops.multiply(mask, _circle_mask(size, record_radius, blur=0.8))

    # White highlight with low alpha
    highlight = Image.new("RGBA", (size, size), (255, 255, 255, 45))
    overlay.paste(highlight, (0, 0), mask)
    return overlay

# ---- shared helpers (dedupe + precompute frame-invariant layers) ----
def _colorize_disc(tex, rarity):
    # Same hue as the shared palette, deepened: this is the dark end of a
    # gradient running up to #3a3a3a, so a pastel here would invert the disc.
    black = aesthetics.disc_color(rarity) if rarity in aesthetics.RARITY_COLOR else "#0D0D0D"
    return ImageOps.colorize(tex, black=black, white="#3a3a3a")

def _build_static_vinyl_layers(size, rarity, label_ratio, hole_ratio):
    """Build every layer that does NOT change between frames, exactly once.
    Previously the disc texture, ring, hole and specular highlight were rebuilt
    inside the per-frame loop."""
    cx = cy = size // 2
    record_radius = int(0.48 * size)

    tex = _vinyl_groove_texture(size, record_radius)
    disc_base = _colorize_disc(tex, rarity).convert("RGBA")
    disc_base.putalpha(_circle_mask(size, record_radius, blur=0.8))

    label_radius = int(record_radius * label_ratio / 2)
    label_mask = Image.new("L", (label_radius * 2, label_radius * 2), 0)
    ImageDraw.Draw(label_mask).ellipse((0, 0, label_radius * 2, label_radius * 2), fill=255)

    # ring + hole merged into one static overlay (drawn above the rotating label)
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(overlay)
    for t, a in [(3, 160), (8, 90)]:
        bbox = (cx - label_radius - t, cy - label_radius - t,
                cx + label_radius + t, cy + label_radius + t)
        dr.ellipse(bbox, outline=(20, 20, 20, a), width=2)
    hole_radius = max(2, int(record_radius * hole_ratio / 2))
    hole = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(hole).ellipse(
        (cx - hole_radius, cy - hole_radius, cx + hole_radius, cy + hole_radius),
        fill=(10, 10, 10, 255))
    overlay.alpha_composite(hole.filter(ImageFilter.GaussianBlur(0.6)))

    return {
        "cx": cx, "cy": cy, "record_radius": record_radius,
        "disc_base": disc_base, "overlay": overlay,
        "label_radius": label_radius, "label_mask": label_mask,
        "highlight_base": _specular_highlight(size, record_radius),
    }

# How much larger than its final size the label is rotated before being scaled
# down. Rotating at the output size and rotating the full 500px cover look the
# same once shrunk, but supersampling keeps the margin so a busy cover cannot
# go soft.
LABEL_SUPERSAMPLE = 2

# The specular sheen turns by angle * 0.1 -- 35 degrees over a whole loop, in
# steps under one degree, on an image that has already been through a 16px
# Gaussian blur. Rotating it per frame was the single most expensive thing here
# and produced differences invisible to the eye, so it is quantised to this many
# distinct angles and each one is built once.
HIGHLIGHT_STEPS = 8


def _render_vinyl_frames(art, layers, num_frames, signature=None, sig_pos=None):
    """Per frame: rotate the label art + reuse a cached highlight rotation.

    The label used to be produced by rotating the full-size cover and then
    scaling it down to the label circle, every frame -- rotating roughly nine
    times the pixels that survive. The cover is scaled once up front instead.
    """
    cx, cy = layers["cx"], layers["cy"]
    label_radius, label_mask = layers["label_radius"], layers["label_mask"]
    disc_base, overlay, highlight_base = layers["disc_base"], layers["overlay"], layers["highlight_base"]

    label_size = label_radius * 2
    source = art.resize((label_size * LABEL_SUPERSAMPLE,) * 2, Image.LANCZOS)
    highlight_step = 36 / HIGHLIGHT_STEPS      # the sheen's full travel is 36 degrees
    highlight_cache = {}

    frames = []
    for frame in range(num_frames):
        angle = (frame / num_frames) * 360
        canvas = disc_base.copy()

        rotated = source.rotate(angle, resample=Image.BICUBIC, expand=False)
        label = rotated.resize((label_size, label_size), Image.LANCZOS).convert("RGBA")
        label.putalpha(label_mask)
        canvas.paste(label, (cx - label_radius, cy - label_radius), label)

        canvas.alpha_composite(overlay)

        bucket = round(angle * 0.1 / highlight_step)
        if bucket not in highlight_cache:
            highlight_cache[bucket] = highlight_base.rotate(
                bucket * highlight_step, resample=Image.BICUBIC, expand=False)
        canvas.alpha_composite(highlight_cache[bucket])

        if signature is not None:
            canvas.alpha_composite(signature, sig_pos)
        frames.append(canvas)
    return frames

def _save_gif(frames, output_path, duration, return_bytes):
    save_kwargs = dict(save_all=True, append_images=frames[1:], duration=duration, loop=0)
    buffer = None
    if return_bytes:
        buffer = BytesIO()
        frames[0].save(buffer, format="GIF", **save_kwargs)
        buffer.seek(0)
    if output_path:
        frames[0].save(output_path, format="GIF", **save_kwargs)
    return buffer

def vinyl_static_bytes(url, output_size=800, rarity="default"):
    """Static (non-animated) vinyl PNG returned as an in-memory BytesIO."""
    img = vinyl_art_from_url(url, output_path=None, output_size=output_size, rarity=rarity)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

def vinyl_art_from_url(
    url: str,
    output_path: str | None = None,
    output_size: int = 1600,
    label_ratio: float = 0.70,
    hole_ratio: float = 0.03,
    rarity = "default"
) -> Image.Image:
    """
    Turn an image URL into a vinyl-record style graphic.

    Args:
        url: Image URL.
        output_path: Where to save the result (e.g., 'vinyl.png'). If None, just return.
        output_size: Final square size in pixels.
        label_ratio: Diameter of the center label relative to disc diameter.
        hole_ratio: Diameter of the center hole relative to disc diameter.

    Returns:
        PIL.Image.Image in RGBA mode.
    """
    # 1) Fetch + square-crop the art
    art = _download_image(url)
    art = _center_crop_square(art)
    art = art.resize((output_size, output_size), Image.LANCZOS)

    # 2) Base canvas
    size = output_size
    cx = cy = size // 2
    record_radius = int(0.48 * size)   # small margin so the disc doesn't touch the edge
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 3) Create vinyl disc texture (grayscale), colorize near-black, clip to circle
    tex = _vinyl_groove_texture(size, record_radius)
    if rarity == "ultimate":
        disc_rgb = ImageOps.colorize(tex, black="#FFE01A", white="#3a3a3a")
    elif rarity == "legendary":
        disc_rgb = ImageOps.colorize(tex, black="#FF9500", white="#3a3a3a")
    elif rarity == "elite":
        disc_rgb = ImageOps.colorize(tex, black="#D60A0A", white="#3a3a3a")
    elif rarity == "unique":
        disc_rgb = ImageOps.colorize(tex, black="#5E23E8", white="#3a3a3a")
    elif rarity == "basic":
        disc_rgb = ImageOps.colorize(tex, black="#0E4BD1", white="#3a3a3a")
    else:
        disc_rgb = ImageOps.colorize(tex, black="#0D0D0D", white="#3a3a3a")
    disc_alpha = _circle_mask(size, record_radius, blur=0.8)
    disc = disc_rgb.convert("RGBA")
    disc.putalpha(disc_alpha)
    canvas.alpha_composite(disc)

    # 4) Center label from the artwork
    label_radius = int(record_radius * label_ratio / 2)
    label = art.resize((label_radius * 2, label_radius * 2), Image.LANCZOS)
    label_mask = Image.new("L", (label_radius * 2, label_radius * 2), 0)
    ImageDraw.Draw(label_mask).ellipse((0, 0, label_radius * 2, label_radius * 2), fill=255)
    label_rgba = label.convert("RGBA")
    label_rgba.putalpha(label_mask)
    canvas.paste(label_rgba, (cx - label_radius, cy - label_radius), label_rgba)

    # 5) Label rings for realism
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(ring)
    for t, a in [(3, 160), (8, 90)]:
        bbox = (cx - label_radius - t, cy - label_radius - t,
                cx + label_radius + t, cy + label_radius + t)
        dr.ellipse(bbox, outline=(20, 20, 20, a), width=2)
    canvas.alpha_composite(ring)

    # 6) Center hole
    hole_radius = max(2, int(record_radius * hole_ratio / 2))
    hole = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dh = ImageDraw.Draw(hole)
    dh.ellipse((cx - hole_radius, cy - hole_radius, cx + hole_radius, cy + hole_radius),
               fill=(10, 10, 10, 255))
    hole = hole.filter(ImageFilter.GaussianBlur(0.6))
    canvas.alpha_composite(hole)

    # 7) Specular highlight overlay
    canvas.alpha_composite(_specular_highlight(size, record_radius))

    if output_path:
        canvas.save(output_path, "PNG")
    return canvas

def vinyl_gif_art_from_url(
    url: str,
    output_path: str | None = None,
    output_size: int = 800,
    label_ratio: float = 0.70,
    hole_ratio: float = 0.03,
    rarity = "default",
    num_frames: int = 36,
    duration: int = 50,
    return_bytes: bool = False,
):
    """
    Create an animated spinning vinyl record GIF from an image URL.

    All frame-invariant layers (disc, rings, hole, specular highlight) are built
    once via `_build_static_vinyl_layers`; only the label art rotates per frame.

    Returns a BytesIO (GIF) when return_bytes=True, else None (writes to output_path).
    """
    if output_path is None and not return_bytes:
        raise ValueError("output_path or return_bytes is required for GIF generation")

    art = _download_image(url)
    art = _center_crop_square(art)
    art = art.resize((output_size, output_size), Image.LANCZOS)

    layers = _build_static_vinyl_layers(output_size, rarity, label_ratio, hole_ratio)
    frames = _render_vinyl_frames(art, layers, num_frames)
    return _save_gif(frames, output_path, duration, return_bytes)

def vinyl_gif_sig_art_from_url(
    url: str,
    artist: str,
    output_path: str | None = None,
    output_size: int = 800,
    label_ratio: float = 0.70,
    hole_ratio: float = 0.03,
    rarity="default",
    num_frames: int = 36,
    duration: int = 50,
    return_bytes: bool = False,
):
    """
    Create an animated spinning vinyl record GIF with a (static) signature overlay.

    Same precomputed-layer approach as `vinyl_gif_art_from_url`; the signature is
    composited at a fixed position each frame.

    Returns a BytesIO (GIF) when return_bytes=True, else None (writes to output_path).
    """
    signature_path = find_signature(artist)
    if signature_path is None:
        raise FileNotFoundError(
            f"No signature art for {artist!r} -- expected "
            f"{signature_filename(artist)} in {SIGS_DIR}")

    if output_path is None and not return_bytes:
        raise ValueError("output_path or return_bytes is required for GIF generation")

    art = _download_image(url)
    art = _center_crop_square(art)
    art = art.resize((output_size, output_size), Image.LANCZOS)

    signature = Image.open(signature_path).convert("RGBA")
    sig_width = int(output_size * (0.4 if artist == "KATSEYE" else 0.3))
    sig_height = int(signature.height * (sig_width / signature.width))
    signature = signature.resize((sig_width, sig_height), Image.LANCZOS)

    layers = _build_static_vinyl_layers(output_size, rarity, label_ratio, hole_ratio)
    sig_x = layers["cx"] - (sig_width // 2)
    sig_y = layers["cy"] - (sig_height // 2) + int(output_size * 0.32)
    frames = _render_vinyl_frames(art, layers, num_frames, signature=signature, sig_pos=(sig_x, sig_y))
    return _save_gif(frames, output_path, duration, return_bytes)

if __name__ == "__main__":
    rarity = "elite"
    
    # Create static vinyl image
    # img = vinyl_art_from_url(
    #     url = "https://i.scdn.co/image/ab67616d0000b273adc16f7df1d7a810a859cd86",
    #     output_path="vinyl_test.png",
    #     output_size=1600,
    #     rarity=rarity
    # )
    
    # # Create animated spinning vinyl GIF
    # vinyl_gif_art_from_url(
    #     url = "https://i.scdn.co/image/ab67616d0000b273adc16f7df1d7a810a859cd86",
    #     output_path="spinning_vinyl.gif",
    #     output_size=800,  # Smaller size for better GIF performance
    #     rarity=rarity,
    #     num_frames=24,    # 24 frames for smooth animation
    #     duration=150      # 150ms per frame (about 6.7 FPS) - slower spin
    # )

    # Create animated spinning vinyl GIF with signature
    vinyl_gif_sig_art_from_url(
        url = "https://i.scdn.co/image/ab67616d0000b273adc16f7df1d7a810a859cd86",
        artist = "The Weeknd",
        output_path="spinning_vinyl_sig.gif",
        output_size=800,  # Smaller size for better GIF performance
        rarity=rarity,
        num_frames=24,    # 24 frames for smooth animation
        duration=150      # 150ms per frame (about 6.7 FPS) - slower spin
    )

