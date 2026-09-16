import asyncio

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

import db
from utils.helpers import add_artist_to_db

catalog_bp = Blueprint("catalog", __name__, url_prefix="/catalog")

# Rarest first -- the order the donut, its legend and the rarity filter all
# present tiers in. Shared with the bot via utils/aesthetics.py, which is also
# where "unassigned" is defined: not a tier, but a selectable state, since the
# panel is where an admin would put a wrongly-tiered song back in the queue.
from utils.aesthetics import RARITY_ORDER, UNASSIGNED, normalise_rarity  # noqa: E402

# Commonest tier first: the select is used most for the bulk of the catalogue.
RARITIES = list(reversed(RARITY_ORDER)) + [UNASSIGNED]


def _wants_json():
    """True when the caller is the page's fetch(), not a plain form post.

    The rarity control still works as a normal <form> submit without JS, so the
    endpoint has to serve both shapes.
    """
    return request.accept_mimetypes.best == "application/json"


@catalog_bp.get("/artists")
def artists():
    q = request.args.get("q", "").strip().lower()
    all_artists = db.get_artist_overview()
    if q:
        all_artists = [a for a in all_artists if q in a["artist"].lower()]
    return render_template(
        "catalog_artists.html",
        artists=all_artists,
        q=request.args.get("q", ""),
        # An empty catalog and a search that matched nothing need different
        # empty states, so the template needs to tell them apart.
        catalog_empty=not db.get_all_artists(),
    )


@catalog_bp.post("/artists/add")
def add_artist():
    artist_name = request.form.get("artist_name", "").strip()
    if not artist_name:
        flash("Artist name is required.", "error")
        return redirect(url_for("catalog.artists"))

    async def _import_artist():
        album_details = await add_artist_to_db(artist_name, "album")
        single_details = await add_artist_to_db(album_details["artist_name"], "single")
        return album_details, single_details

    album_details, single_details = asyncio.run(_import_artist())
    corrected_name = album_details["artist_name"]
    flash(
        f"Imported {corrected_name}: "
        f"{album_details['albums_saved']} albums / {album_details['songs_saved']} songs, "
        f"{single_details['albums_saved']} singles / {single_details['songs_saved']} songs.",
        "success",
    )
    return redirect(url_for("catalog.artist_detail", artist=corrected_name))


@catalog_bp.get("/artists/<artist>")
def artist_detail(artist):
    songs = db.get_songs_with_ids_by_artist(artist)
    rarity_counts = dict(db.get_song_rarity_counts_by_artist(artist))

    albums = {}
    for song_id, song_name, rarity, album_name, track_count, album_image, album_id in songs:
        album = albums.get(album_id)
        if album is None:
            # Only derive the category once per album, not once per track.
            album = albums[album_id] = {
                "id": album_id,
                "name": album_name,
                "category": db.get_album_category(track_count),
                "image": album_image,
                "songs": [],
            }
        album["songs"].append({"id": song_id, "name": song_name, "rarity": rarity})

    # Albums first, then EPs, then singles -- the order an admin scans in. The
    # SQL already ordered by name, so equal-category albums stay alphabetical.
    order = {"Album": 0, "EP": 1, "Single": 2}
    album_list = sorted(albums.values(), key=lambda a: (order.get(a["category"], 3), a["name"].lower()))

    # Album / EP / Single counts drive the workbench's type filter, so it can
    # label each option and hide any type this artist has none of.
    type_counts = {}
    for album in album_list:
        type_counts[album["category"]] = type_counts.get(album["category"], 0) + 1

    return render_template(
        "catalog_artist_detail.html",
        artist=artist,
        albums=album_list,
        type_counts=type_counts,
        stats=db.get_artist_collection_stats(artist),
        popularity=db.get_artist_popularity(artist),
        rarity_order=RARITY_ORDER,
        # Distinct songs, not joined rows: a song on both a standard and a
        # deluxe edition appears twice in `songs`, and counting rows would
        # disagree with the catalog card and the donut for the same artist.
        song_count=len({s[0] for s in songs}),
        rarity_counts=rarity_counts,
        rarities=RARITIES,
    )


@catalog_bp.post("/artists/<artist>/songs/<song_id>/rarity")
def set_song_rarity(artist, song_id):
    # strict=True: this is a write path, so a value the palette cannot name is
    # rejected here rather than stored for every reader to cope with later.
    try:
        rarity = normalise_rarity(request.form.get("rarity"), strict=True)
    except ValueError as exc:
        if _wants_json():
            return jsonify(ok=False, error=str(exc)), 400
        flash(str(exc), "error")
        return redirect(url_for("catalog.artist_detail", artist=artist))

    try:
        db.set_song_rarity(song_id, rarity)
    except Exception as exc:  # surfaced in the row so the admin can retry
        if _wants_json():
            return jsonify(ok=False, error=str(exc)), 500
        raise

    if _wants_json():
        return jsonify(ok=True, song_id=song_id, rarity=rarity)

    flash("Rarity updated.", "success")
    return redirect(url_for("catalog.artist_detail", artist=artist))


@catalog_bp.post("/artists/<artist>/remove")
def remove_artist(artist):
    if request.form.get("confirm") != "yes":
        flash("Removal not confirmed.", "error")
        return redirect(url_for("catalog.artist_detail", artist=artist))

    db.remove_songs_by_artist(artist)
    flash(f"Removed all songs and albums for {artist}.", "success")
    return redirect(url_for("catalog.artists"))
