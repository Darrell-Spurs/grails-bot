from flask import Blueprint, render_template

from utils import odds as game_odds

odds_bp = Blueprint("odds", __name__, url_prefix="/odds")


@odds_bp.get("")
def index():
    """Reference page for the tunable game probabilities.

    Every value is read live from utils/odds.py rather than restated here, so
    the page cannot drift from what the bot actually rolls -- which is the
    failure this whole change set exists to prevent.
    """
    variant_rows = []
    for label, per_pull, note in (
        ("Mythic", game_odds.MYTHIC_PER_PULL,
         "Limited to " + str(game_odds.MYTHIC_MAX_COPIES) + " copies server-wide; claimed by button."),
        ("Guts Cookie", game_odds.GUTSCOOKIE_PER_PULL,
         "Replaces the pull entirely and awards " + f"{game_odds.SOUR_PATCH_GUTSCOOKIE_XP:,}" + " XP."),
        ("Sketch", game_odds.SKETCH_PER_PULL, "Pencil-effect artwork."),
        ("Glitched", game_odds.GLITCHED_PER_PULL, "Datamosh-effect artwork."),
    ):
        variant_rows.append({
            "label": label,
            "per_pull": per_pull,
            "per_song": game_odds.per_song_chance(per_pull),
            "note": note,
        })

    # Whatever probability is left over after the four special variants is a
    # plain card, so it is derived rather than configured.
    leftover = 1.0
    for row in variant_rows:
        leftover -= row["per_song"]

    rarity_tables = [
        ("Normal pull", "", "Used by .c and by an ordinary vinyl pull."),
        ("Signature pull", "sig_vinyl",
         "Used by .sv, and by the " + f"{game_odds.SIG_VINYL_CHANCE:.0%}"
         + " of vinyl pulls that upgrade themselves."),
    ]

    tables = []
    for title, key, note in rarity_tables:
        rates = game_odds.RARITY_RATES[key]
        tables.append({
            "title": title,
            "note": note,
            "total": sum(rates.values()),
            "rows": [(r, rates[r]) for r in game_odds.RARITY_ORDER if r in rates],
        })

    return render_template(
        "odds.html",
        variant_rows=variant_rows,
        default_per_song=leftover,
        tables=tables,
        songs_per_pull=game_odds.SONGS_PER_PULL,
        sour_patch=game_odds.SOUR_PATCH_ODDS,
        sour_patch_split=game_odds.SOUR_PATCH_VINYL_SPLIT,
        sig_vinyl_chance=game_odds.SIG_VINYL_CHANCE,
        mythic_max_copies=game_odds.MYTHIC_MAX_COPIES,
    )
