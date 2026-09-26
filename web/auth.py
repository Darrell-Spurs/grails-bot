import hmac
import logging
import os
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from flask import Blueprint, request, session, redirect, url_for, render_template

auth_bp = Blueprint("auth", __name__)
log = logging.getLogger("grails.web")

# Wrong passwords one address may try before it has to wait. Generous on
# purpose: the password is shared by the admins, so this is here to stop a
# script guessing all day, not to lock out someone who mistyped a few times.
MAX_FAILURES = 10
WINDOW_SECONDS = 10 * 60

_failures = {}          # address -> deque of recent failure times
_failures_lock = threading.Lock()


def _recent(address, now):
    """Failures from `address` inside the window, oldest first (pruned)."""
    times = _failures.get(address)
    if times is None:
        return deque()
    while times and now - times[0] > WINDOW_SECONDS:
        times.popleft()
    if not times:
        del _failures[address]
    return times


def _safe_next(target):
    """Where to go after logging in: a path on this site, never another site.

    The old code redirected to whatever `next` held, so a crafted login link
    could send an admin off to a look-alike page right after they logged in.
    """
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    parts = urlsplit(target)
    return None if parts.scheme or parts.netloc else target


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        address = request.remote_addr or "?"
        now = time.monotonic()
        with _failures_lock:
            recent = _recent(address, now)
            wait = WINDOW_SECONDS - (now - recent[0]) if len(recent) >= MAX_FAILURES else 0
        if wait > 0:
            minutes = max(1, round(wait / 60))
            error = f"Too many wrong passwords. Try again in {minutes} minute{'s' if minutes != 1 else ''}."
            return render_template("login.html", error=error), 429

        password = request.form.get("password", "")
        expected = os.environ.get("ADMIN_PASSWORD") or ""
        # compare_digest takes as long for a near miss as for a wild guess.
        if expected and hmac.compare_digest(password.encode(), expected.encode()):
            with _failures_lock:
                _failures.pop(address, None)
            session.clear()
            session["authed"] = True
            return redirect(_safe_next(request.args.get("next")) or url_for("dashboard.index"))

        with _failures_lock:
            _failures.setdefault(address, deque()).append(now)
        log.warning("failed admin panel login from %s", address)
        error = "Incorrect password."
    return render_template("login.html", error=error)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
