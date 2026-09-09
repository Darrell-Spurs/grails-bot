import os
import logging
import secrets

from flask import Flask, session, request, redirect, url_for

from web.auth import auth_bp
from web.blueprints.health import health_bp
from web.blueprints.dashboard import dashboard_bp
from web.blueprints.catalog import catalog_bp
from web.blueprints.users import users_bp
from web.blueprints.mythic import mythic_bp

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

    app.register_blueprint(auth_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(catalog_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(mythic_bp)

    @app.before_request
    def require_login():
        if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
            return None
        if not session.get("authed"):
            return redirect(url_for("auth.login", next=request.path))
        return None

    return app
