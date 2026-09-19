"""The single source of truth for how rarities and variants look.

Before this module the same vocabulary was written out in eight places -- two
tables inside helpers.py alone, plus cogs, Jinja macros and three blocks of CSS
custom properties. A palette change had to be made everywhere at once or the
bot and the admin panel would disagree about what "legendary" looks like.

Nothing here imports from the project, and nothing here imports discord, so it
is safe to pull into cogs, image helpers and the Flask app alike. Discord-facing
callers convert with rarity_colour_int() / variant_colour_int().
"""
import colorsys
import logging

log = logging.getLogger("grails.aesthetics")

# ---------------------------------------------------------------------------
# Song rarity
# ---------------------------------------------------------------------------

# Rarest first. This order drives sorting, legends and every listing.
RARITY_ORDER = ["ultimate", "legendary", "elite", "unique", "basic"]

# Not a tier: the state a song is in between being imported from Spotify and an
# admin assigning it a rarity. Deliberately absent from RARITY_ORDER and from
# every RARITY_* table below, so it can never appear in a legend, a donut chart,
# a filter menu or the card-emoji matrix -- and, because the draw tables name
# only real tiers, an unassigned song can never be pulled.
#
# It doubles as the fallback for any value the palette does not recognise.
# Rendering must not throw over a cosmetic lookup, and quietly borrowing a real
# tier's colour would misrepresent what a card is worth; showing it as plainly
# unassigned is honest, and _warn_once() makes sure it is noticed.
UNASSIGNED = "unassigned"
UNASSIGNED_LABEL = "unassigned"
UNASSIGNED_COLOR = "#8b93a1"        # neutral grey, unmistakably not a tier
UNASSIGNED_DOT = "⬜"           # white large square

RARITY_COLOR = {
    "ultimate":  "#ffeb83",
    "legendary": "#ffc79a",
    "elite":     "#ff6060",
    "unique":    "#c09aff",
    "basic":     "#9ae1ff",
}

# One glyph per tier, used everywhere a rarity is shown on its own. There used
# to be a second thematic set for headings; two vocabularies for one idea meant
# the same tier looked different depending on which surface you were on.
RARITY_DOT = {
    "ultimate":  "\U0001F7E1",  # yellow circle
    "legendary": "\U0001F7E0",  # orange circle
    "elite":     "\U0001F534",  # red circle
    "unique":    "\U0001F7E3",  # purple circle
    "basic":     "\U0001F535",  # blue circle
}


# ---------------------------------------------------------------------------
# Collection variant
# ---------------------------------------------------------------------------

# Most desirable first: this order groups a collection listing.
VARIANTS = ["mythic", "sig_vinyl", "vinyl", "sketch", "glitched", "default"]

VARIANT_LABEL = {
    "mythic": "Mythic", "sig_vinyl": "Signature", "vinyl": "Vinyl",
    "sketch": "Sketch", "glitched": "Glitched", "default": "Standard",
}


VARIANT_COLOR = {
    "mythic":    "#22d3ee",
    "sig_vinyl": "#fb7185",
    "vinyl":     "#a78bfa",
    "sketch":    "#fbbf24",
    "glitched":  "#a3e635",
    "default":   "#94a3b8",
}

# Accent used when nothing rarity-specific applies.
ACCENT_COLOR = "#5b4bdb"
ACCENT_COLOR_INT = 0x5b4bdb

# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def _key(value, default):
    return (value or default).lower()


_warned_rarities = set()


def _warned(rarity):
    """Normalise a rarity for lookup, logging the first sight of a bad value.

    Deduplicated through a set: rendering a 500-row catalogue page with one
    broken row should produce one log line, not five hundred.
    """
    key = _key(rarity, UNASSIGNED)
    if key != UNASSIGNED and key not in RARITY_COLOR and key not in _warned_rarities:
        _warned_rarities.add(key)
        log.warning("Unknown rarity %r; rendering it as %s", rarity, UNASSIGNED)
    return key


def rarity_rank(rarity):
    """Sort key: rarest first, unknown tiers last."""
    try:
        return RARITY_ORDER.index(_key(rarity, UNASSIGNED))
    except ValueError:
        return len(RARITY_ORDER)


def variant_rank(variant):
    """Sort key: most desirable first, unknown variants last."""
    try:
        return VARIANTS.index(_key(variant, "default"))
    except ValueError:
        return len(VARIANTS)


def rarity_hex(rarity):
    return RARITY_COLOR.get(_warned(rarity), UNASSIGNED_COLOR)


def variant_hex(variant):
    return VARIANT_COLOR.get(_key(variant, "default"), ACCENT_COLOR)


def hex_to_int(value):
    """'#ffeb83' -> 0xffeb83, for discord.Colour and PIL alike."""
    return int(str(value).lstrip("#"), 16)


def rarity_colour_int(rarity):
    return hex_to_int(rarity_hex(rarity))


def variant_colour_int(variant):
    return hex_to_int(variant_hex(variant))


def rarity_dot(rarity):
    return RARITY_DOT.get(_warned(rarity), UNASSIGNED_DOT)


def rarity_emoji(rarity):
    """Alias of rarity_dot(): one glyph per tier, everywhere."""
    return rarity_dot(rarity)


def variant_emoji(variant):
    """The guild emoji for a variant on its own, or "" if it is not uploaded.

    There is deliberately no unicode stand-in: a wrong-but-present glyph reads
    as intentional, while an empty one is visibly a missing upload.
    """
    v = _key(variant, "default")
    return named_emoji(VARIANT_EMOJI_NAME.get(v, v), default="")


def is_rarity(value):
    """True only for one of the five real tiers."""
    return (value or "").lower() in RARITY_COLOR


def normalise_rarity(value, strict=False):
    """Canonicalise a rarity for storage.

    Display code never wants strict=True: a lookup that throws would take down
    a whole command over a colour. The write paths -- the admin rarity command,
    the catalogue importer, the web panel -- do, because a bad value accepted
    there is a bad value every reader has to cope with forever.

    Raises ValueError when strict and the value is neither a tier nor the
    unassigned sentinel; otherwise returns UNASSIGNED for anything unknown.
    """
    key = (value or "").strip().lower()
    if key in RARITY_COLOR or key == UNASSIGNED:
        return key
    if strict:
        raise ValueError(
            f"{value!r} is not a rarity. Expected one of: "
            + ", ".join(RARITY_ORDER) + f", {UNASSIGNED}")
    return UNASSIGNED


def variant_label(variant):
    v = _key(variant, "default")
    return VARIANT_LABEL.get(v, v)


# ---------------------------------------------------------------------------
# Card emoji: one glyph per (rarity, variant) pair
#
# The server carries a custom emoji for every combination, named
# "<rarity>_<variant>" -- "unique_glitch" -- with the plain rarity name used for
# the standard variant ("basic"). IDs are never hardcoded: the bot hands this
# module its guild emojis on ready and they are resolved by name, so renaming or
# re-uploading one in Discord needs no code change. Anything unresolved falls
# back to the unicode glyphs below, which is what a fresh server or a failed
# upload will show.
# ---------------------------------------------------------------------------

# Discord emoji names allow [A-Za-z0-9_] only, so the variant key is shortened
# where the natural name would not round-trip.
VARIANT_EMOJI_SUFFIX = {
    "mythic": "mythic",
    "sig_vinyl": "sig",
    "vinyl": "vinyl",
    "sketch": "sketch",
    "glitched": "glitch",
    "default": "",          # the standard variant uses the bare rarity name
}

# The guild emoji for a variant shown on its own (the filter menu), as opposed
# to the rarity x variant card glyphs above.
VARIANT_EMOJI_NAME = {
    "mythic": "mythic",
    "sig_vinyl": "sig_vinyl",
    "vinyl": "vinyl",
    "sketch": "sketch",
    "glitched": "glitch",
    "default": "default",
}

_card_emojis = {}
_card_emoji_provider = None


def set_card_emoji_provider(fn):
    """Register a callable returning the live {name: emoji} mapping.

    set_card_emojis() alone was a one-shot snapshot taken at on_ready. If the
    guild cache was empty or late at that moment, every card silently rendered a
    generic fallback for the rest of the process's life, with nothing to say why.
    With a provider the registry can refill itself on the first miss, so a bad
    snapshot costs one lookup instead of a restart.
    """
    global _card_emoji_provider
    _card_emoji_provider = fn


def _refill():
    """Re-ask the provider. Only called when the registry looks wrong."""
    if _card_emoji_provider is None:
        return False
    try:
        mapping = _card_emoji_provider()
    except Exception:
        log.exception("card emoji provider failed")
        return False
    if not mapping:
        return False
    set_card_emojis(mapping)
    return True


def card_emoji_names(rarity, variant):
    """Names to look for, best first.

    Several spellings are tried so the server can use either the full variant
    key or the short suffix without the code caring which.
    """
    r = _key(rarity, UNASSIGNED)
    v = _key(variant, "default")
    short = VARIANT_EMOJI_SUFFIX.get(v, v)

    names = []
    if short:
        names.append(f"{r}_{short}")
        if short != v:
            names.append(f"{r}_{v}")
    else:
        names.append(r)
        names.append(f"{r}_default")
    return names


def set_card_emojis(mapping):
    """Register the guild's custom emoji as {name: renderable}.

    Called once the bot is ready. Replacing rather than updating means a
    deleted emoji stops being served after a reconnect.
    """
    global _card_emojis
    _card_emojis = {str(k).lower(): v for k, v in (mapping or {}).items()}
    log.info("Registered %d custom card emoji", len(_card_emojis))


def registered_card_emojis():
    """What is currently resolvable -- used by diagnostics and tests."""
    return dict(_card_emojis)


def card_emoji(rarity, variant="default"):
    """The single glyph representing a card's rarity *and* variant."""
    names = card_emoji_names(rarity, variant)
    for name in names:
        found = _card_emojis.get(name.lower())
        if found:
            return str(found)

    # An empty registry means the snapshot never arrived, not that this pair is
    # genuinely missing. Refill once and retry before giving up; a registry that
    # is merely incomplete is left alone, so this cannot run on every render.
    if not _card_emojis and _refill():
        for name in names:
            found = _card_emojis.get(name.lower())
            if found:
                return str(found)
    # Last resort is the rarity dot: variant glyphs are guild uploads now and
    # may be absent, while the dot is always present.
    return rarity_dot(rarity)


# ---------------------------------------------------------------------------
# Standalone guild emoji
#
# Anything the bot refers to by name that is not part of the rarity x variant
# matrix -- the vinyl disc, a mascot, a currency icon. Resolved the same way:
# by name at startup, never by hardcoded id, with a unicode fallback so a
# missing upload degrades instead of printing a broken ":vinyl:".
#
# Add a name here and the loader will start looking for it; nothing else needs
# to change.
# ---------------------------------------------------------------------------

# Names the bot asks the guild for. No unicode stand-ins: an emoji that is not
# uploaded renders as nothing, which is visibly a gap rather than a glyph that
# looks deliberate. `.emojicheck` lists what is missing.
NAMED_EMOJI = {"vinyl", "sig_vinyl", "cookie", "sourpatch"} | set(VARIANT_EMOJI_NAME.values())

_named_emojis = {}


def set_named_emojis(mapping):
    """Register the standalone guild emoji as {name: renderable}."""
    global _named_emojis
    _named_emojis = {str(k).lower(): v for k, v in (mapping or {}).items()}


def named_emoji(name, default=""):
    """The guild emoji called `name`, or `default` when it is not uploaded.

    Safe to call before the bot is ready -- it simply returns `default`.
    """
    found = _named_emojis.get((name or "").lower())
    return str(found) if found else default


def wanted_emoji_names():
    """Every emoji name the bot wants from the guild -- cards and standalone.

    The loader filters the guild's emoji down to this set, so unrelated server
    emoji cost nothing and cannot shadow one the bot depends on.
    """
    names = {n.lower()
             for r in RARITY_ORDER
             for v in VARIANTS
             for n in card_emoji_names(r, v)}
    names.update(NAMED_EMOJI)
    return names


def missing_card_emojis():
    """(rarity, variant) pairs with no custom emoji registered.

    Lets an admin see exactly which uploads are outstanding instead of
    discovering it one card at a time.
    """
    missing = []
    for r in RARITY_ORDER:
        for v in VARIANTS:
            if not any(_card_emojis.get(n.lower()) for n in card_emoji_names(r, v)):
                missing.append((r, v))
    return missing


# ---------------------------------------------------------------------------
# Discord select-option emoji safety
# ---------------------------------------------------------------------------

# Characters that default to TEXT presentation, so a trailing U+FE0F is part of
# their emoji sequence. Everything else is already emoji-presentation and must
# not carry one.
TEXT_PRESENTATION_BASES = {
    "✏",      # pencil
    "\U0001F58B",  # lower left ballpoint pen
    "❤",      # heavy black heart
    "✉",      # envelope
    "⚠",      # warning sign
    "✒",      # black nib
}


def safe_option_emoji(value):
    """Return `value` if it is safe as a Discord select-option emoji, else None.

    Losing one glyph is invisible; sending a non-RGI sequence makes Discord
    reject the entire message, which once took out /collection completely.
    """
    if not value:
        return None
    if value.startswith("<") and value.endswith(">"):
        return value          # a custom guild emoji, not a unicode sequence
    if "️" in value and value[0] not in TEXT_PRESENTATION_BASES:
        log.warning(
            "Dropping select-option emoji %r: U+FE0F on a base that is already "
            "emoji-presentation is not an RGI sequence and Discord rejects it.",
            value,
        )
        return None
    return value


def shade(hex_color, lightness=0.45, saturation=0.90):
    """Return the same hue at a fixed lightness/saturation.

    Lets a surface with different contrast needs stay on-palette instead of
    keeping its own colour table.
    """
    r, g, b = (int(hex_color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, _l, _s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, lightness, saturation)
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def disc_color(rarity):
    """Deep tone for the vinyl groove art.

    The main palette is tuned for embeds and reads as pastel; it is used here as
    the *dark* end of a gradient running up to #3a3a3a, so it has to be darker
    than the light end or the disc inverts. Same hue, deeper tone.
    """
    return shade(rarity_hex(rarity), lightness=0.45, saturation=0.90)


def css_variables(indent="  "):
    """Emit the palette as CSS custom properties.

    The admin panel renders this into its <head> rather than restating the
    colours in style.css, so the stylesheet and the bot cannot drift apart.
    """
    lines = [f"{indent}--r-{name}: {value};" for name, value in RARITY_COLOR.items()]
    lines.append(f"{indent}--r-{UNASSIGNED}: {UNASSIGNED_COLOR};")
    lines += [f"{indent}--v-{name}: {value};" for name, value in VARIANT_COLOR.items()]
    return "\n".join(lines)
