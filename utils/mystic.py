from PIL import Image, ImageDraw, ImageFilter, ImageFont
import requests

from io import BytesIO
import numpy as np
import math
import random
import os

# Resolved here rather than imported from helpers: helpers imports this
# module, so reaching back for it would be a circular import.
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

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
_BADGE_RATIO = 0.08          # pill height as a share of the canvas


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
    height = int(size * _BADGE_RATIO)
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
    canvas.alpha_composite(halo.filter(
        ImageFilter.GaussianBlur(max(4, round(height * 0.18)))))

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
