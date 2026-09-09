import asyncio

from flask import Blueprint, render_template, request, redirect, url_for, flash

import db
from utils.helpers import add_artist_to_db

catalog_bp = Blueprint("catalog", __name__, url_prefix="/catalog")

RARITIES = ["basic", "unique", "elite", "legendary", "ultimate"]


@catalog_bp.get("/artists")
def artists():
    q = request.args.get("q", "").strip().lower()
    all_artists = db.get_all_artists()
    if q:
        all_artists = [a for a in all_artists if q in a.lower()]
    return render_template("catalog_artists.html", artists=all_artists, q=request.args.get("q", ""))


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
    for song_id, song_name, rarity, album_name, track_count in songs:
        albums.setdefault(album_name, {"category": db.get_album_category(track_count), "songs": []})
        albums[album_name]["songs"].append({"id": song_id, "name": song_name, "rarity": rarity})

    return render_template(
        "catalog_artist_detail.html",
        artist=artist,
        albums=albums,
        rarity_counts=rarity_counts,
        rarities=RARITIES,
    )


@catalog_bp.post("/artists/<artist>/songs/<song_id>/rarity")
def set_song_rarity(artist, song_id):
    rarity = request.form.get("rarity")
    if rarity not in RARITIES:
        flash("Invalid rarity.", "error")
    else:
        db.set_song_rarity(song_id, rarity)
        flash("Rarity updated.", "success")
    return redirect(url_for("catalog.artist_detail", artist=artist))


@catalog_bp.post("/artists/<artist>/remove")
def remove_artist(artist):
    if request.form.get("confirm") != "yes":
        flash("Removal not confirmed.", "error")
        return redirect(url_for("catalog.artist_detail", artist=artist))

    db.remove_songs_by_artist(artist)
    flash(f"Removed all songs/albums for {artist}.", "success")
    return redirect(url_for("catalog.artists"))
