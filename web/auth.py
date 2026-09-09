import os
from functools import wraps
from flask import Blueprint, request, session, redirect, url_for, render_template

auth_bp = Blueprint("auth", __name__)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password and password == os.environ.get("ADMIN_PASSWORD"):
            session.clear()
            session["authed"] = True
            return redirect(request.args.get("next") or url_for("dashboard.index"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
