from flask import Blueprint, render_template
import db

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.get("/")
def index():
    conn = db.get_connection()
    c = conn.cursor()
    song_count = c.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    album_count = c.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
    user_count = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()

    latest = db.list_latest(limit=10)
    hunt = db.get_mythic_hunt()

    return render_template(
        "dashboard.html",
        song_count=song_count,
        album_count=album_count,
        user_count=user_count,
        latest=latest,
        hunt=hunt,
    )
