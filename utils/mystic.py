from PIL import Image, ImageDraw, ImageFilter
import requests
from io import BytesIO
from collections import Counter
import numpy as np
import functools
import math

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