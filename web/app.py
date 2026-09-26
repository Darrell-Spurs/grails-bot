import os
import logging
import secrets
from datetime import datetime

from flask import Flask, session, request, redirect, url_for

from utils import aesthetics
from web.auth import auth_bp
from web.blueprints.health import health_bp
from web.blueprints.dashboard import dashboard_bp
from web.blueprints.catalog import catalog_bp
from web.blueprints.users import users_bp
from web.blueprints.mythic import mythic_bp
from web.blueprints.odds import odds_bp

# Endpoints reachable without a login
PUBLIC_ENDPOINTS = {"auth.login", "health.health", "static"}


log = logging.getLogger("grails.web")


def create_app(bot=None):
    app = Flask(__name__)

    # Never fall back to a fixed, publicly-known key: anyone who knew it could
    # forge an authenticated session cookie. An ephemeral random key is safe --
    # it only means existing logins don't survive a restart.
    secret = os.environ.get("FLASK_SECRET_KEY")
    if not secret:
        secret = secrets.token_hex(32)
        log.warning(
            "FLASK_SECRET_KEY is not set - using a random key for this run. "
            "Sessions will not survive a restart. Set it in .env to fix."
        )
    app.secret_key = secret
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.bot = bot

    # Jinja *globals*, not a context processor: macros imported with
    # `{% from "_macros.html" import ... %}` do not receive the calling
    # template's context, so a context processor is invisible inside them.
    # Globals are visible everywhere, including imported macros.
    app.jinja_env.globals.update(
        RARITY_ORDER=aesthetics.RARITY_ORDER,
        UNASSIGNED=aesthetics.UNASSIGNED,
        RARITY_COLOR=aesthetics.RARITY_COLOR,
        VARIANTS=aesthetics.VARIANTS,
        VARIANT_LABEL=aesthetics.VARIANT_LABEL,
        VARIANT_COLOR=aesthetics.VARIANT_COLOR,
        palette_css=aesthetics.css_variables(indent="      "),
    )

    @app.template_filter("moment")
    def _moment(value):
        """Render a collected_at value as 'YYYY-MM-DD HH:MM'.

        Postgres hands back a datetime and SQLite a string, and the raw value
        carries microseconds that are noise in a table. Anything unparseable is
        passed through untouched rather than swallowed.
        """
        if value is None:
            return "—"
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M")
        text = str(value)
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                continue
        return text

    app.register_blueprint(auth_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(catalog_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(mythic_bp)
    app.register_blueprint(odds_bp)

    @app.after_request
    def security_headers(response):
        # Cheap hardening now that the panel can be reachable from the
        # internet: it cannot be framed by another site (clickjacking), the
        # browser does not guess content types, and full panel URLs are not
        # sent to the image and font hosts the pages load from.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.before_request
    def require_login():
        if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
            return None
        if not session.get("authed"):
            return redirect(url_for("auth.login", next=request.path))
        return None

    return app
