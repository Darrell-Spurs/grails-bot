from flask import Blueprint, render_template, request, redirect, url_for, flash

import db

mythic_bp = Blueprint("mythic", __name__, url_prefix="/mythic")


@mythic_bp.get("")
def index():
    hunt = db.get_mythic_hunt()
    q = request.args.get("q", "").strip()
    results = db.search_songs(q, limit=20) if q else []
    return render_template("mythic.html", hunt=hunt, results=results, q=q)


@mythic_bp.post("/set")
def set_hunt():
    song_id = request.form.get("song_id")
    song_name = request.form.get("song_name")
    artist = request.form.get("artist")
    album_id = request.form.get("album_id")
    album_name = request.form.get("album_name")
    album_url = request.form.get("album_url")

    if not song_id or not album_id:
        flash("Pick a song with a linked album to set as the hunt target.", "error")
        return redirect(url_for("mythic.index"))

    db.set_mythic_hunt(song_id, song_name, album_id, album_name, album_url, artist)
    flash(f"Mythic hunt set to {song_name} by {artist}.", "success")
    return redirect(url_for("mythic.index"))


@mythic_bp.post("/clear")
def clear_hunt():
    db.clear_mythic_hunt()
    flash("Mythic hunt cleared.", "success")
    return redirect(url_for("mythic.index"))


@mythic_bp.get("/status/<song_id>")
def status(song_id):
    info = db.get_mythic_copy_info(song_id)
    owners = db.get_mythic_owners(song_id)
    return render_template("mythic_status.html", song_id=song_id, info=info, owners=owners)
