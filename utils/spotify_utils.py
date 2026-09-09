import os
import time
import base64
import threading

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")

# ---- cached client-credentials token ----
# Spotify tokens live ~1 hour; re-fetching on every call wasted ~140ms each time.
_token_cache = {"value": None, "expires_at": 0.0}
_token_lock = threading.Lock()
_EXPIRY_MARGIN = 60  # refresh a minute early to avoid using a just-expired token


def _fetch_token():
    auth = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth.encode()).decode()
    headers = {"Authorization": f"Basic {b64_auth}"}
    data = {"grant_type": "client_credentials"}
    res = requests.post("https://accounts.spotify.com/api/token", headers=headers, data=data)
    payload = res.json()
    token = payload.get("access_token")
    expires_in = payload.get("expires_in", 3600)
    return token, expires_in


def get_access_token(force_refresh: bool = False):
    """Return a Spotify access token, reusing the cached one until it nears expiry."""
    now = time.time()
    # Fast path: valid cached token (no lock needed for a simple read).
    if not force_refresh and _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]

    with _token_lock:
        # Re-check inside the lock in case another thread just refreshed it.
        now = time.time()
        if not force_refresh and _token_cache["value"] and now < _token_cache["expires_at"]:
            return _token_cache["value"]
        token, expires_in = _fetch_token()
        _token_cache["value"] = token
        _token_cache["expires_at"] = time.time() + expires_in - _EXPIRY_MARGIN
        return token
