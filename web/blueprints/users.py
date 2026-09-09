from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

import db

users_bp = Blueprint("users", __name__, url_prefix="/users")


def _find_user(user_id):
    for row in db.get_all_users():
        if str(row[0]) == str(user_id):
            return {"id": row[0], "username": row[1], "xp": row[2], "vinyl_count": row[3], "sig_vinyl_count": row[4]}
    return None


@users_bp.get("")
def index():
    q = request.args.get("q", "").strip().lower()
    rows = db.get_all_users()
    if q:
        rows = [r for r in rows if q in str(r[0]).lower() or q in (r[1] or "").lower()]
    return render_template("users.html", users=rows, q=request.args.get("q", ""))


@users_bp.get("/latest")
def latest():
    n = request.args.get("n", 25, type=int)
    songs = db.list_latest(limit=n)
    return render_template("users_latest.html", songs=songs, n=n)


@users_bp.post("/latest/remove")
def remove_latest():
    n = request.form.get("n", 1, type=int)
    removed = db.remove_latest(n=n)
    flash(f"Removed {removed} most recent pull(s).", "success")
    return redirect(url_for("users.latest"))


@users_bp.get("/<user_id>")
def detail(user_id):
    user = _find_user(user_id)
    if not user:
        abort(404)
    collection = db.get_collection_with_copy_numbers(user_id)
    return render_template("user_detail.html", user=user, collection=collection)


@users_bp.post("/<user_id>/xp")
def adjust_xp(user_id):
    delta = request.form.get("delta", 0, type=int)
    if not db.user_exists(user_id):
        flash("User is not registered.", "error")
    else:
        db.add_user_xp(user_id, delta)
        flash(f"Applied {delta:+d} XP.", "success")
    return redirect(url_for("users.detail", user_id=user_id))


@users_bp.post("/<user_id>/vinyl")
def adjust_vinyl(user_id):
    delta = request.form.get("delta", 0, type=int)
    if not db.user_exists(user_id):
        flash("User is not registered.", "error")
    elif abs(delta) > 10000:
        flash("Amount must be between -10000 and 10000.", "error")
    else:
        db.add_vinyl_count(user_id, delta)
        flash(f"Applied {delta:+d} vinyl pulls.", "success")
    return redirect(url_for("users.detail", user_id=user_id))


@users_bp.post("/<user_id>/sigvinyl")
def adjust_sig_vinyl(user_id):
    delta = request.form.get("delta", 0, type=int)
    if not db.user_exists(user_id):
        flash("User is not registered.", "error")
    elif abs(delta) > 10000:
        flash("Amount must be between -10000 and 10000.", "error")
    else:
        db.add_sig_vinyl_count(user_id, delta)
        flash(f"Applied {delta:+d} signature vinyl pulls.", "success")
    return redirect(url_for("users.detail", user_id=user_id))


@users_bp.post("/<user_id>/unregister")
def unregister(user_id):
    if request.form.get("confirm") != "yes":
        flash("Removal not confirmed.", "error")
        return redirect(url_for("users.detail", user_id=user_id))

    db.unregister_user(user_id)
    flash(f"Unregistered user {user_id}.", "success")
    return redirect(url_for("users.index"))
