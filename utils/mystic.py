from PIL import Image, ImageDraw, ImageFilter, ImageFont
import requests

from io import BytesIO
from collections import Counter
import numpy as np
import functools
import math
import random
import os

# Resolved here rather than imported from helpers: helpers imports this
# module, so reaching back for it would be a circular import.
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

def get_dominant_color(image, resize=100):
    """
    Get the dominant non-grayscale color from an image using average method
    Filters out gray/neutral colors to capture the real vibrant color
    """
    # Resize for faster processing
    img = image.copy()
    if img.mode == 'RGBA':
        # Convert RGBA to RGB on white background
        background = Image.new('RGB', img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])  # Use alpha channel as mask
        img = background
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    
    img = img.resize((resize, resize))
    
    # Get all pixels
    pixels = list(img.getdata())
    
    # Filter out grayscale/neutral colors
    def is_colorful(pixel, threshold=15):
        """Check if a pixel has enough color variation to not be grayscale"""
        r, g, b = pixel
        # Calculate the difference between max and min RGB values
        color_range = max(r, g, b) - min(r, g, b)
        # Also check if the color is not too dark or too bright
        brightness = (r + g + b) / 3
        return color_range > threshold and 20 < brightness < 235
    
    # Filter pixels to only colorful ones
    colorful_pixels = [pixel for pixel in pixels if is_colorful(pixel)]
    
    # If no colorful pixels found, fall back to all pixels
    if not colorful_pixels:
        colorful_pixels = pixels
    
    # Calculate the average color of colorful pixels
    pixels_array = np.array(colorful_pixels)
    avg_color = tuple(pixels_array.mean(axis=0).astype(int))
    return avg_color
    
def make_color_vibrant(color, saturation_boost=1.2, brightness_boost=1.5):
    """
    Make a color more vibrant by boosting saturation and brightness
    
    Args:
        color: RGB tuple (r, g, b)
        saturation_boost: Factor to increase saturation (1.0 = no change)
        brightness_boost: Factor to increase brightness (1.0 = no change)
    
    Returns:
        tuple: Enhanced RGB color
    """
    r, g, b = color
    
    # Convert RGB to HSV for easier saturation manipulation
    r_norm, g_norm, b_norm = r/255.0, g/255.0, b/255.0
    
    max_val = max(r_norm, g_norm, b_norm)
    min_val = min(r_norm, g_norm, b_norm)
    diff = max_val - min_val
    
    # Calculate HSV values
    # Hue
    if diff == 0:
        h = 0
    elif max_val == r_norm:
        h = (60 * ((g_norm - b_norm) / diff) + 360) % 360
    elif max_val == g_norm:
        h = (60 * ((b_norm - r_norm) / diff) + 120) % 360
    else:
        h = (60 * ((r_norm - g_norm) / diff) + 240) % 360
    
    # Saturation
    s = 0 if max_val == 0 else diff / max_val
    
    # Value (brightness)
    v = max_val
    
    # Boost saturation and brightness
    s = min(s * saturation_boost, 1.0)
    v = min(v * brightness_boost, 1.0)
    
    # Convert back to RGB
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    
    if 0 <= h < 60:
        r_prime, g_prime, b_prime = c, x, 0
    elif 60 <= h < 120:
        r_prime, g_prime, b_prime = x, c, 0
    elif 120 <= h < 180:
        r_prime, g_prime, b_prime = 0, c, x
    elif 180 <= h < 240:
        r_prime, g_prime, b_prime = 0, x, c
    elif 240 <= h < 300:
        r_prime, g_prime, b_prime = x, 0, c
    else:
        r_prime, g_prime, b_prime = c, 0, x
    
    # Convert back to 0-255 range
    r_final = int((r_prime + m) * 255)
    g_final = int((g_prime + m) * 255)
    b_final = int((b_prime + m) * 255)
    
    return (r_final, g_final, b_final)

def make_color_glowy(color, lightness_factor=2.4, glow_intensity=0.8):
    """
    Make a color much lighter and glowy by increasing brightness significantly
    
    Args:
        color: RGB tuple (r, g, b)
        lightness_factor: Factor to increase lightness (higher = lighter)
        glow_intensity: How much to preserve the original hue (0-1)
    
    Returns:
        tuple: Glowy RGB color
    """
    r, g, b = color
    
    # Convert to HSV for better control
    r_norm, g_norm, b_norm = r/255.0, g/255.0, b/255.0
    
    max_val = max(r_norm, g_norm, b_norm)
    min_val = min(r_norm, g_norm, b_norm)
    diff = max_val - min_val
    
    # Calculate HSV values
    if diff == 0:
        h = 0
    elif max_val == r_norm:
        h = (60 * ((g_norm - b_norm) / diff) + 360) % 360
    elif max_val == g_norm:
        h = (60 * ((b_norm - r_norm) / diff) + 120) % 360
    else:
        h = (60 * ((r_norm - g_norm) / diff) + 240) % 360
    
    s = 0 if max_val == 0 else diff / max_val
    v = max_val
    
    # Make it glowy: reduce saturation and increase brightness dramatically
    s_glow = s * glow_intensity  # Reduce saturation for glow effect
    v_glow = min(v * lightness_factor, 1.0)  # Increase brightness significantly
    
    # Convert back to RGB
    c = v_glow * s_glow
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v_glow - c
    
    if 0 <= h < 60:
        r_prime, g_prime, b_prime = c, x, 0
    elif 60 <= h < 120:
        r_prime, g_prime, b_prime = x, c, 0
    elif 120 <= h < 180:
        r_prime, g_prime, b_prime = 0, c, x
    elif 180 <= h < 240:
        r_prime, g_prime, b_prime = 0, x, c
    elif 240 <= h < 300:
        r_prime, g_prime, b_prime = x, 0, c
    else:
        r_prime, g_prime, b_prime = c, 0, x
    
    # Convert back to 0-255 range
    r_final = int((r_prime + m) * 255)
    g_final = int((g_prime + m) * 255)
    b_final = int((b_prime + m) * 255)
    
    return (r_final, g_final, b_final)

def _compose_static_glow(base_img, glowy_color, intensity=0.8):
    """Album cover + a *static* radial glow. Computed once and reused for every frame
    (the expensive part: 25 ellipses + GaussianBlur(15) that used to run per frame)."""
    glow_layer = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow_layer)
    center = (base_img.width // 2, base_img.height // 2)
    max_radius = 250
    for r in range(250, 0, -10):
        alpha = int(255 * (r / max_radius) * 0.15 * intensity)
        draw.ellipse(
            [center[0] - r, center[1] - r, center[0] + r, center[1] + r],
            fill=glowy_color + (alpha,),
        )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(15))
    return Image.alpha_composite(glow_layer, base_img)



# How far apart the border is sampled. Each sample paints a segment this long,
# so raising it trades a little gradient smoothness for a lot of Python work.
_SAMPLE_STEP = 8


# ---------------------------------------------------------------------------
# Border colour cache
#
# _draw_wave_border asks for a colour once per sample, per layer, per side --
# about 3,500 RGB<->HLS conversions a frame, or 100,000 for a whole GIF, and it
# asks for the same handful of colours over and over as the wave sweeps round.
# Quantising the factors to two decimals collapses that to a few hundred
# distinct keys; the rounding is far below what a 24-bit channel can express, so
# the pixels come out identical.
#
# The cache is keyed on the album's own glow colour, so two albums never share
# an entry. maxsize bounds it across a long-running process.
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=8192)
def _cached_border_color(glowy_color, saturation_boost, enhanced_factor, enhanced_glow):
    vibrant = make_color_vibrant(glowy_color, saturation_boost, enhanced_factor)
    return make_color_glowy(vibrant, enhanced_factor, enhanced_glow)


def _border_color(glowy_color, saturation_boost, enhanced_factor, enhanced_glow):
    return _cached_border_color(tuple(glowy_color), round(saturation_boost, 2),
                                round(enhanced_factor, 2), round(enhanced_glow, 2))


def _draw_wave_border(mythic_img, glowy_color, progress):
    """Draw the animated counter-clockwise wave border onto `mythic_img` in place.
    This is the only part that changes between frames."""
    frame_draw = ImageDraw.Draw(mythic_img)
    width, height = mythic_img.size
    border_thickness = 10

    # The border is sampled every STEP pixels and each sample draws a segment
    # STEP long. The two must stay tied together: with a step wider than the
    # segment, the segments stop touching and the border breaks into stripes.
    step = _SAMPLE_STEP
    seg = step - 1

    perimeter = 2 * (width + height - 4)
    wave_length = perimeter / 1.5
    wave_position = (progress * perimeter) % perimeter

    def calculate_wave_intensity(pos):
        dist1 = abs(pos - wave_position)
        dist2 = perimeter - dist1
        distance = min(dist1, dist2)
        if distance <= wave_length / 2:
            wave_factor = math.cos(math.pi * distance / (wave_length / 2))
            if wave_factor >= 0:
                smooth_factor = wave_factor ** 0.7
                return 0.6 + (1.0 - 0.6) * smooth_factor
            else:
                smooth_factor = (abs(wave_factor)) ** 1.2
                return 0.6 - (0.6 - 0.3) * smooth_factor
        return 0.4

    for border_layer in range(border_thickness):
        layer_offset = border_layer * 0.1
        for side in ['top', 'right', 'bottom', 'left']:
            if side == 'top':
                start_pos, end_pos, y_coord = 0, width - 1, border_layer
            elif side == 'right':
                start_pos, end_pos, x_coord = width - 1, width + height - 2, width - 1 - border_layer
            elif side == 'bottom':
                start_pos, end_pos, y_coord = width + height - 2, 2 * width + height - 3, height - 1 - border_layer
            else:
                start_pos, end_pos, x_coord = 2 * width + height - 3, perimeter, border_layer

            side_length = end_pos - start_pos

            # Walk each side over its own pixel span rather than over the
            # perimeter arithmetic above. The four side_lengths do not add up to
            # four full edges -- the left one comes out five short -- and
            # floor division then dropped the final partial segment, so every
            # edge stopped a few pixels before its corner and the top-left
            # corner was left with a visible notch. Ceiling division over the
            # true span puts the last segment on the corner instead.
            span = width if side in ('top', 'bottom') else height
            for i in range(max(1, -(-span // step))):
                offset = min(i * step, span - 1)
                sample_pos = (start_pos + offset) % perimeter
                intensity = calculate_wave_intensity(sample_pos + layer_offset)

                if intensity < 0.6:
                    transition_factor = intensity / 0.6
                    enhanced_factor = 0.7 + transition_factor * 0.5
                    enhanced_glow = 0.3 + transition_factor * 0.3
                    saturation_boost = 1.8 + transition_factor * 0.4
                else:
                    transition_factor = (intensity - 0.6) / 0.4
                    enhanced_factor = 1.2 + transition_factor * 2.8
                    enhanced_glow = 0.6 + transition_factor * 0.2
                    saturation_boost = 1.2 + transition_factor * 0.3

                border_color = _border_color(glowy_color, saturation_boost,
                                             enhanced_factor, enhanced_glow)
                alpha = int(255 * (0.8 + intensity * 0.2))
                color = border_color + (alpha,)
                line_w = 2 if border_layer < 8 else 1

                if side == 'top':
                    px = offset
                    frame_draw.line([(px, y_coord), (min(px + seg, width - 1), y_coord)], fill=color, width=line_w)
                elif side == 'right':
                    py = offset
                    frame_draw.line([(x_coord, py), (x_coord, min(py + seg, height - 1))], fill=color, width=line_w)
                elif side == 'bottom':
                    px = width - 1 - offset
                    frame_draw.line([(max(px - seg, 0), y_coord), (px, y_coord)], fill=color, width=line_w)
                else:
                    py = height - 1 - offset
                    frame_draw.line([(x_coord, max(py - seg, 0)), (x_coord, py)], fill=color, width=line_w)
    return mythic_img


def create_mythic_album_gif(url, output_path=None, frames=30, duration=100, size=(300, 300), return_bytes=False):
    """
    Create an animated mythic GIF: a *static* album cover + glow with only the border animating.

    Args:
        url: URL of the album image
        output_path: Path to save the GIF, or None to skip writing to disk
        frames: Number of animation frames (default 30; was 60)
        duration: ms per frame
        size: image size
        return_bytes: if True, also return an in-memory BytesIO (GIF) for direct discord.File use

    Returns:
        BytesIO (GIF) if return_bytes else the first PIL frame.
    """
    response = requests.get(url)
    base_img = Image.open(BytesIO(response.content)).convert("RGBA").resize(size)

    dominant_color = get_dominant_color(base_img)
    glowy_color = make_color_glowy(dominant_color, 2.2, 0.6)

    # Static cover + glow, built ONCE (used to be rebuilt every frame).
    static_bg = _compose_static_glow(base_img, glowy_color, intensity=0.8)

    animation_frames = []
    for i in range(frames):
        progress = i / frames
        frame = static_bg.copy()
        _draw_wave_border(frame, glowy_color, progress)
        animation_frames.append(frame)

    save_kwargs = dict(save_all=True, append_images=animation_frames[1:],
                       duration=duration, loop=0, optimize=True, format="GIF")

    buffer = None
    if return_bytes:
        buffer = BytesIO()
        animation_frames[0].save(buffer, **save_kwargs)
        buffer.seek(0)
    if output_path:
        animation_frames[0].save(output_path, **{k: v for k, v in save_kwargs.items() if k != "format"})

    return buffer if return_bytes else animation_frames[0]

# ---------------------------------------------------------------------------
# Mythic card: the still image /view shows for a mythic
#
# A blurred, zoomed crop of the cover fills the frame; the cover itself sits
# smaller in the middle with a pale border; the copy number rides in a badge top
# right; glitter is scattered over the blurred margin only, so it never lands on
# the artwork.
# ---------------------------------------------------------------------------

MYTHIC_CARD_SIZE = 500
_INSET_RATIO = 0.70          # cover width as a share of the canvas
_BLUR_RADIUS = 26
_BLUR_ZOOM = 1.35            # crop in before blurring, so no edge pixels smear
_GLITTER_COUNT = 90


def _mix(a, b, t):
    """Blend two RGB tuples; t=0 keeps the first, t=1 gives the second."""
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def _theme_rgb(rarity):
    """The rarity's palette colour as RGB, from utils/aesthetics.py."""
    from utils import aesthetics
    value = aesthetics.rarity_hex(rarity).lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _blurred_backdrop(cover, size):
    """The cover, zoomed and blurred, as the full-bleed background."""
    zoom = int(size * _BLUR_ZOOM)
    back = cover.resize((zoom, zoom), Image.LANCZOS)
    off = (zoom - size) // 2
    back = back.crop((off, off, off + size, off + size))
    back = back.filter(ImageFilter.GaussianBlur(_BLUR_RADIUS))
    # Knocked back so the sharp cover and the glitter read against it.
    return Image.blend(back, Image.new("RGB", (size, size), (26, 22, 30)), 0.28)


def _scatter_glitter(canvas, keep_out, rng, theme=(255, 246, 214)):
    """Sparkles on the blurred margin, never over the cover.

    Drawn as a dot plus a faint cross, which reads as a glint at this size
    without the cost of a real star polygon or a per-sparkle blur.
    """
    size = canvas.size[0]
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    left, top, right, bottom = keep_out

    placed = 0
    for _ in range(_GLITTER_COUNT * 3):        # oversample; most land on the cover
        if placed >= _GLITTER_COUNT:
            break
        x = rng.randint(4, size - 5)
        y = rng.randint(4, size - 5)
        if left - 6 < x < right + 6 and top - 6 < y < bottom + 6:
            continue
        placed += 1

        radius = rng.choice((1, 1, 1, 2, 2, 3))
        alpha = rng.randint(90, 255)
        # Mostly the rarity colour, lightened, with white mixed in so the
        # field does not read as a single flat tint.
        tinted = rng.random() < 0.6
        colour = (_mix(theme, (255, 255, 255), 0.45) if tinted else (255, 255, 255)) + (alpha,)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=colour)

        if radius >= 2:                        # a glint only on the bigger ones
            arm = radius * 3
            faint = (colour[0], colour[1], colour[2], alpha // 3)
            draw.line((x - arm, y, x + arm, y), fill=faint)
            draw.line((x, y - arm, x, y + arm), fill=faint)

    canvas.alpha_composite(layer)


def _copy_badge(canvas, text, centre, theme):
    """The `#n` pill, centred on `centre`, tinted with the rarity colour.

    Drawn on a 4x layer and scaled down: PIL's rounded_rectangle has no
    anti-aliasing, so at badge size the corners come out visibly stepped.
    Supersampling is the cheapest fix -- one extra resize on a small layer.
    """
    size = canvas.size[0]
    height = int(size * 0.1)
    font = _font_for(int(height * 0.46))
    text_w = ImageDraw.Draw(canvas).textlength(text, font=font)
    width = max(int(text_w + height * 0.9), height)
    cx, cy = centre
    box = (cx - width // 2, cy - height // 2, cx + width // 2, cy + height // 2)

    # A faint seat rather than a glow: enough to lift the pill off a busy
    # backdrop, not so much that it competes with the cover's own halo.
    halo = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(halo).rounded_rectangle(box, radius=height // 2,
                                           fill=_mix(theme, (0, 0, 0), 0.45) + (70,))
    canvas.alpha_composite(halo.filter(ImageFilter.GaussianBlur(9)))

    SS = 4
    w, h = width * SS, height * SS
    pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    # A vertical ramp between two dark tints of the theme colour. Both ends sit
    # well below the cover in brightness so the pill reads as a solid tag rather
    # than a second light source; the rarity still comes through as the hue.
    ramp = Image.new("RGBA", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        ramp.putpixel((0, y), tuple(
            int(a + (b - a) * t) for a, b in
            zip(_mix(theme, (18, 14, 22), 0.42), _mix(theme, (12, 9, 15), 0.86))
        ) + (255,))
    ramp = ramp.resize((w, h))

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=255)
    pill.paste(ramp, (0, 0), mask)

    ring = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle(
        (SS, SS, w - 1 - SS, h - 1 - SS), radius=h // 2,
        outline=_mix(theme, (10, 8, 13), 0.15) + (215,), width=2 * SS)
    pill.alpha_composite(ring)

    canvas.alpha_composite(pill.resize((width, height), Image.LANCZOS), (box[0], box[1]))

    # Text last and at full resolution: scaling it down with the pill would
    # soften the glyphs for no benefit.
    ImageDraw.Draw(canvas).text(
        (box[0] + (width - text_w) / 2, box[1] + height * 0.31), text,
        font=font, fill=(255, 252, 244, 255))


def _font_for(px):
    for name in ("calibrib.ttf", "arialbd.ttf", "calibri.ttf"):
        path = os.path.join(FONTS_DIR, name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                pass
    return ImageFont.load_default()


def mythic_card_bytes(url, copy_number=None, size=MYTHIC_CARD_SIZE, seed=None,
                      rarity=None):
    """The mythic still, as an in-memory PNG.

    `seed` makes the glitter deterministic, so re-viewing the same card gives
    the same picture instead of reshuffling every time. `rarity` picks the glow,
    glitter and badge colour from the shared palette, so an ultimate mythic and
    a basic one are tellable apart at a glance.
    """
    theme = _theme_rgb(rarity)
    response = requests.get(url, timeout=15)
    cover = Image.open(BytesIO(response.content)).convert("RGB")
    side = min(cover.size)
    left = (cover.width - side) // 2
    top = (cover.height - side) // 2
    cover = cover.crop((left, top, left + side, top + side))

    canvas = _blurred_backdrop(cover, size).convert("RGBA")

    inset = int(size * _INSET_RATIO)
    x0 = (size - inset) // 2
    y0 = (size - inset) // 2          # centred on both axes
    keep_out = (x0, y0, x0 + inset, y0 + inset)

    _scatter_glitter(canvas, keep_out, random.Random(seed), theme)

    # Shadow first, so the cover appears to sit above the blur.
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle(
        (x0 + 6, y0 + 10, x0 + inset + 6, y0 + inset + 10), fill=(0, 0, 0, 120))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))

    # Two blurred rectangles instead of a drawn border: a wide faint one for the
    # halo bleeding into the blur, a tight bright one for the edge itself. Both
    # are single Gaussian passes, so the radiance costs a couple of
    # milliseconds rather than a per-pixel falloff.
    for spread, alpha, blur in ((int(size * 0.075), 110, 26), (int(size * 0.022), 210, 9)):
        glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).rectangle(
            (x0 - spread, y0 - spread, x0 + inset + spread, y0 + inset + spread),
            fill=_mix(theme, (255, 255, 255), 0.25) + (alpha,))
        canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(blur)))

    canvas.paste(cover.resize((inset, inset), Image.LANCZOS), (x0, y0))

    if copy_number:
        # Centred on the cover's top-right corner, so it sits half over the
        # artwork and half over the glow.
        _copy_badge(canvas, f"#{copy_number}", (x0 + inset, y0), theme)

    buffer = BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


# ---------------------------------------------------------------------------
# Mythic card, animated
#
# The same composition as the still -- deliberately, so a card does not change
# shape when it moves. Only the two things that are already light sources move:
# the sparkles twinkle and drift, and the cover's halo breathes.
#
# The backdrop, shadow, cover and badge are rendered once and reused. A
# Gaussian blur per layer per frame would be the entire cost of the GIF;
# rescaling a finished layer's alpha is a lookup table.
# ---------------------------------------------------------------------------

_ANIM_FRAMES = 24
_ANIM_DURATION = 80          # ms per frame, so one loop runs just under 2s
_GLOW_SWING = 0.65           # how far the halo alpha travels either side of base
_DRIFT_RADIUS = 2.4          # px each sparkle orbits over one loop

# Base alphas, not peak ones: the pulse swings _GLOW_SWING either side of these,
# so the halo runs 0.35x..1.65x of what is written here -- roughly a five-fold
# range between the dimmest and brightest frame.
#
# The bases sit well below the still's (110 and 210) for that reason. Raising
# them instead of widening the swing would only clip: the tight edge already
# peaks at 248, and anything above 255 flattens the top of every pulse into a
# hold, which reads as a stutter rather than a brighter glow.
_ANIM_GLOW_LAYERS = ((0.075, 85, 26), (0.022, 150, 9))


def _dither_once(image, strength=2, seed=0):
    """Break up quantisation banding with noise that is the same every frame.

    A GIF has 256 colours, and the blurred backdrop is a smooth gradient, so
    quantising it flat leaves visible contour rings. Floyd-Steinberg fixes the
    rings but is computed per frame, so the noise *crawls* -- and, worse, it
    makes every pixel differ from the frame before, which took this animation
    from 670 KB to 2.7 MB.

    Adding a fixed noise field before quantising gets the same dither for none
    of that: the backdrop is identical in all 24 frames, so it costs almost
    nothing after the first, and nothing appears to move where nothing is.
    """
    noise = np.random.default_rng(seed).integers(
        -strength, strength + 1, (image.size[1], image.size[0], 1))
    return Image.fromarray(
        np.clip(np.asarray(image, np.int16) + noise, 0, 255).astype(np.uint8))


def _glitter_field(size, keep_out, rng, theme):
    """Fixed sparkle positions, each with a phase, so a frame is a cheap redraw.

    Every sparkle gets its own phase and its own *integer* harmonic. The phases
    stop the field pulsing in unison; the harmonics being whole numbers is what
    makes the loop seamless, since each sparkle is back where it started on the
    frame after the last one.
    """
    left, top, right, bottom = keep_out
    # Wider keep-out than the still needs: a sparkle drifts, and one that
    # wanders onto the artwork breaks the illusion that the cover sits above.
    margin = 6 + int(math.ceil(_DRIFT_RADIUS))
    field = []
    for _ in range(_GLITTER_COUNT * 3):
        if len(field) >= _GLITTER_COUNT:
            break
        x = rng.randint(4, size - 5)
        y = rng.randint(4, size - 5)
        if left - margin < x < right + margin and top - margin < y < bottom + margin:
            continue
        tinted = rng.random() < 0.6
        field.append({
            "x": x, "y": y,
            "radius": rng.choice((1, 1, 1, 2, 2, 3)),
            "alpha": rng.randint(90, 255),
            "rgb": _mix(theme, (255, 255, 255), 0.45) if tinted else (255, 255, 255),
            "phase": rng.uniform(0, 2 * math.pi),
            "harmonic": rng.choice((1, 1, 2, 2, 3)),
            "drift": rng.uniform(0, 2 * math.pi),
        })
    return field


def _glitter_frame(size, field, t):
    """One frame of the sparkle layer; `t` runs 0..1 through the loop."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    angle = 2 * math.pi * t
    for s in field:
        # Floors at 0.15 rather than 0: a sparkle that goes fully dark reads as
        # a dropped frame rather than a twinkle.
        swing = 0.575 + 0.425 * math.sin(s["harmonic"] * angle + s["phase"])
        alpha = int(s["alpha"] * swing)
        if alpha <= 4:
            continue
        x = s["x"] + _DRIFT_RADIUS * math.cos(angle + s["drift"])
        y = s["y"] + _DRIFT_RADIUS * math.sin(angle + s["drift"])
        r = s["radius"]
        draw.ellipse((x - r, y - r, x + r, y + r), fill=s["rgb"] + (alpha,))
        if r >= 2:
            # The glint grows and shrinks with the brightness, which is what
            # sells it as a glint rather than a dot fading in place.
            arm = r * 3 * swing
            faint = s["rgb"] + (alpha // 3,)
            draw.line((x - arm, y, x + arm, y), fill=faint)
            draw.line((x, y - arm, x, y + arm), fill=faint)
    return layer


def _scaled_alpha(layer, factor):
    """A copy of `layer` with its alpha multiplied. One LUT pass, no reblur."""
    out = layer.copy()
    out.putalpha(layer.getchannel("A").point(
        lambda v: min(255, int(v * factor))))
    return out


def mythic_card_gif_bytes(url, copy_number=None, size=MYTHIC_CARD_SIZE, seed=None,
                          rarity=None, frames=_ANIM_FRAMES,
                          duration=_ANIM_DURATION, output_path=None):
    """The mythic card as a seamlessly looping animated GIF.

    Same arguments and same look as mythic_card_bytes; `seed` still fixes the
    sparkle layout so the card animates identically every time it is viewed.
    """
    theme = _theme_rgb(rarity)
    response = requests.get(url, timeout=15)
    cover = Image.open(BytesIO(response.content)).convert("RGB")
    side = min(cover.size)
    left = (cover.width - side) // 2
    top = (cover.height - side) // 2
    cover = cover.crop((left, top, left + side, top + side))

    backdrop = _dither_once(_blurred_backdrop(cover, size)).convert("RGBA")

    inset = int(size * _INSET_RATIO)
    x0 = y0 = (size - inset) // 2
    keep_out = (x0, y0, x0 + inset, y0 + inset)
    cover_small = cover.resize((inset, inset), Image.LANCZOS)

    field = _glitter_field(size, keep_out, random.Random(seed), theme)

    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle(
        (x0 + 6, y0 + 10, x0 + inset + 6, y0 + inset + 10), fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))

    glows = []
    for index, (spread_ratio, alpha, blur) in enumerate(_ANIM_GLOW_LAYERS):
        spread = int(size * spread_ratio)
        glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(glow).rectangle(
            (x0 - spread, y0 - spread, x0 + inset + spread, y0 + inset + spread),
            fill=_mix(theme, (255, 255, 255), 0.25) + (alpha,))
        # The wide halo and the tight edge breathe a third of a cycle apart, so
        # the light looks like it is spreading and gathering rather than the
        # whole border simply flashing on and off together.
        glows.append((glow.filter(ImageFilter.GaussianBlur(blur)),
                      index * 2 * math.pi / 3))

    badge = None
    if copy_number:
        badge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        _copy_badge(badge, f"#{copy_number}", (x0 + inset, y0), theme)

    built = []
    for index in range(frames):
        t = index / frames
        frame = backdrop.copy()
        frame.alpha_composite(_glitter_frame(size, field, t))
        frame.alpha_composite(shadow)
        for glow, offset in glows:
            frame.alpha_composite(_scaled_alpha(
                glow, 1.0 + _GLOW_SWING * math.sin(2 * math.pi * t + offset)))
        frame.paste(cover_small, (x0, y0))
        if badge is not None:
            frame.alpha_composite(badge)
        built.append(frame.convert("RGB"))

    # One palette for the whole animation, taken from the brightest frame so the
    # highlights are represented. Quantising each frame on its own lets the
    # palette drift, which shows up as the backdrop crawling even where nothing
    # is actually moving.
    #
    # dither=NONE is the reason this file is a sensible size: _dither_once has
    # already broken the gradients, so the only pixels that differ between
    # frames are the ones that genuinely moved, and the GIF encoder can skip
    # the rest.
    palette = built[frames // 4].quantize(colors=255)
    out = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in built]

    save_kwargs = dict(save_all=True, append_images=out[1:], loop=0,
                       duration=duration, optimize=True)
    if output_path:
        out[0].save(output_path, **save_kwargs)
    buffer = BytesIO()
    out[0].save(buffer, format="GIF", **save_kwargs)
    buffer.seek(0)
    return buffer


def main(url, return_bytes=False):
    """Generate the mythic GIF. Returns a BytesIO (GIF) when return_bytes=True,
    otherwise the first PIL frame (and no longer writes to disk by default)."""
    return create_mythic_album_gif(
        url=url,
        output_path=None,
        frames=30,
        duration=100,
        return_bytes=return_bytes,
    )


# Example usage
if __name__ == "__main__":
    mystic_url = "https://i.scdn.co/image/ab67616d0000b2734ab2520c2c77a1d66b9ee21d"
    main(mystic_url)