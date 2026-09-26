"""Every tunable game probability, in one place.

These numbers were previously written out at each use site -- the rarity table
existed twice in helpers.py alone, which meant a balance change could silently
apply to one pull path and not the other. Import from here instead; nothing in
this module imports anything else, so it is safe to pull into cogs, helpers and
the admin panel alike.

Cost note: these are module-level constants resolved once at import. Reading
one is a dict/attribute lookup measured in nanoseconds, next to the ~86ms a
single database round-trip costs -- so keeping them here is free. What would
*not* be free is loading them from a file or table on every pull; if odds ever
need to be editable at runtime, cache them in memory and reload on change
rather than reading per draw.
"""

# --- Pull shape -------------------------------------------------------------

# How many songs a .c pull offers. The per-song variant odds below are derived
# from the per-pull odds using this.
SONGS_PER_PULL = 3


# --- Variant odds, expressed per pull ---------------------------------------
# "Per pull" means: the chance that a single .c shows at least one song of this
# variant. draw_variant() converts each to a per-song probability.

# MYTHIC_PER_PULL = 1        # testing only: every pull is a mythic
MYTHIC_PER_PULL = 0.005      # 0.5%
GUTSCOOKIE_PER_PULL = 0.02   # 2%
# SKETCH_PER_PULL = 1   # 3%
SKETCH_PER_PULL = 0.03       # 3%
GLITCHED_PER_PULL = 0.12     # 12%


def per_song_chance(per_pull, songs=SONGS_PER_PULL):
    """Convert a per-pull probability into the per-song probability that
    produces it across `songs` independent draws.

    P(at least one in N) = 1 - (1 - p_song)^N, solved for p_song.
    """
    return 1 - (1 - per_pull) ** (1 / songs)


# --- Song rarity weights ----------------------------------------------------
# Keyed by the pull's weight variant: "" is a normal pull, "sig_vinyl" is the
# guaranteed-signature pull, which cannot roll basic.

RARITY_RATES = {
    "": {
        "ultimate": 0.01,
        "legendary": 0.07,
        "elite": 0.17,
        "unique": 0.30,
        "basic": 0.45,
    },
    "sig_vinyl": {
        "ultimate": 0.02,
        "legendary": 0.15,
        "elite": 0.35,
        "unique": 0.48,
        "basic": 0,
    },
}

# Display order comes from the shared palette so odds tables and every listing
# agree on which tier is rarest.
from utils.aesthetics import RARITY_ORDER  # noqa: E402,F401


# --- XP awarded for claiming a card ------------------------------------------
# Final award = VARIANT_XP[variant] * RARITY_XP_MULTIPLIER[rarity].
#
# These used to live in choice_cog while /xphelp restated them by hand, and the
# two had drifted: the help promised 10/50/200/800 against the real
# 30/100/500/1200. Keeping one copy here means the help reads the same numbers
# the game pays out.

VARIANT_XP = {
    "mythic": 1200,
    "sketch": 500,
    "glitched": 100,
    "default": 30,
}

RARITY_XP_MULTIPLIER = {
    "ultimate": 3,
    "legendary": 2.5,
    "elite": 2,
    "unique": 1.5,
    "basic": 1,
}

DAILY_XP_MIN = 100
DAILY_XP_MAX = 1000


# --- Post-pull bonus --------------------------------------------------------

SOUR_PATCH_ODDS = 0.01          # chance of a Sour Patch Kids after any pull
SOUR_PATCH_VINYL_SPLIT = 0.5    # of those, share that grant a vinyl instead of a level-up
SOUR_PATCH_GUTSCOOKIE_XP = 1500 # XP awarded by a Guts Cookie pull


# --- Vinyl pulls ------------------------------------------------------------

SIG_VINYL_CHANCE = 0.10   # chance an ordinary .vinyl pull rolls signature odds


# --- Mythic supply ----------------------------------------------------------

MYTHIC_MAX_COPIES = 5     # copies of any one mythic that can exist server-wide
