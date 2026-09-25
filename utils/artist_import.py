"""Importing an artist's catalogue from Spotify, as a job anyone can watch.

An import used to be a chain of functions that were async in name only: every
Spotify request and every database insert inside them blocked the event loop,
so the whole bot froze for as long as an import ran, and nothing could say how
far along it was. The web panel ran the same chain inside its request, so the
browser simply spun until it finished or timed out.

Now an import is a job:

  * start_import() registers it and runs it on a worker thread. Asking for an
    artist that is already being imported returns the running job instead of
    starting a second one.
  * The job records its phase, a done/total count and any errors as it goes,
    and every surface reads that one record: the status message `.addartist`
    edits in place, the web panel's job page (which polls it as JSON), and the
    log.
  * Each release list is fetched from Spotify once (it used to be fetched
    twice per import), track listings a few releases at a time, and all the
    database writes go through the batched db.save_artist_catalog().
"""
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import db
from utils.logsetup import event
from utils.spotify_utils import get_access_token

log = logging.getLogger("grails.import")

API = "https://api.spotify.com/v1"
MARKET = "US"
GROUPS = ("album", "single")

# Releases whose name contains one of these are skipped, as they always were.
SKIPPED_WORDS = ("live", "mix", "karaoke", "playlist")

# Track listings are fetched this many releases at a time: enough to cut a
# large discography from a minute of requests to a few seconds, few enough to
# stay well clear of Spotify's rate limit.
TRACK_WORKERS = 4
REQUEST_TIMEOUT = 15

# Finished jobs stay readable this long, so a job page opened late -- or left
# open -- still shows how the import ended.
KEEP_FINISHED_SECONDS = 3600

PHASE_LABELS = {
    "queued": "waiting to start",
    "finding": "finding the artist",
    "albums": "listing releases",
    "tracks": "fetching track lists",
    "saving": "saving to the database",
    "done": "finished",
    "failed": "failed",
}


class ImportFailed(Exception):
    """An import that cannot go on, with a message fit to show an admin."""


class ImportJob:
    """One artist import. Written by its worker thread, read by anyone.

    Every read goes through snapshot(), which copies the fields under the lock,
    so the Discord and web readers never see a half-updated job.
    """

    def __init__(self, query, requested_by, source):
        self.id = uuid.uuid4().hex[:12]
        self.query = query
        self.requested_by = requested_by
        self.source = source
        self.artist = None
        self.phase = "queued"
        self.done = 0
        self.total = 0
        self.releases = {group: 0 for group in GROUPS}
        self.tracks = {group: 0 for group in GROUPS}
        self.result = None
        self.errors = []
        self.started_at = time.time()
        self.phase_started_at = self.started_at
        self.finished_at = None
        self.finished = threading.Event()
        self._lock = threading.Lock()

    # ---- written by the worker ----------------------------------------------
    def _set_phase(self, phase, total=0):
        with self._lock:
            self.phase = phase
            self.done, self.total = 0, total
            self.phase_started_at = time.time()
        event(log, "import " + phase, "%s", self.artist or self.query)

    def _advance(self, by=1):
        with self._lock:
            self.done += by

    def _error(self, message):
        with self._lock:
            self.errors.append(message)
        log.warning("import %s: %s", self.artist or self.query, message)

    def _finish(self, phase, result=None, error=None):
        with self._lock:
            self.phase = phase
            self.result = result
            if error:
                self.errors.append(error)
            self.finished_at = time.time()
        self.finished.set()

    # ---- read by everyone ---------------------------------------------------
    @property
    def active(self):
        return not self.finished.is_set()

    def snapshot(self):
        """A consistent, JSON-safe copy of the job as it stands."""
        with self._lock:
            now = self.finished_at or time.time()
            eta = None
            if self.phase == "tracks" and 0 < self.done < self.total:
                per_item = (time.time() - self.phase_started_at) / self.done
                eta = round(per_item * (self.total - self.done))
            return {
                "id": self.id,
                "query": self.query,
                "artist": self.artist,
                "requested_by": self.requested_by,
                "phase": self.phase,
                "phase_label": PHASE_LABELS.get(self.phase, self.phase),
                "done": self.done,
                "total": self.total,
                "releases": dict(self.releases),
                "tracks": dict(self.tracks),
                "result": dict(self.result) if self.result else None,
                "errors": list(self.errors),
                "elapsed": round(now - self.started_at, 1),
                "eta": eta,
                "active": self.finished_at is None,
            }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_jobs = {}
_jobs_lock = threading.Lock()


def _key(query):
    return " ".join(query.lower().split())


def _prune(now):
    for job_id in [j.id for j in _jobs.values()
                   if j.finished_at and now - j.finished_at > KEEP_FINISHED_SECONDS]:
        del _jobs[job_id]


def start_import(query, *, requested_by="", source="discord"):
    """Start importing `query`, or join the import of it already running.

    Returns (job, started): `started` is False when an import of the same
    artist was already under way and `job` is that one.
    """
    query = query.strip()
    with _jobs_lock:
        _prune(time.time())
        for job in _jobs.values():
            if job.active and _key(job.query) == _key(query):
                return job, False
        job = ImportJob(query, requested_by, source)
        _jobs[job.id] = job
    threading.Thread(target=_run, args=(job,), name=f"import-{job.id}", daemon=True).start()
    event(log, "import queued", "%s   by %s (%s)", query, requested_by or "?", source)
    return job, True


# Where each phase sits on one 0-100 bar, so progress never runs backwards
# as a new phase starts. Track listings are the long part of an import.
_PHASE_SPAN = {"queued": (0, 0), "finding": (0, 4), "albums": (4, 12),
               "tracks": (12, 95), "saving": (95, 99), "done": (100, 100), "failed": (100, 100)}


def percent(snap):
    """0-100 for a progress bar, from a snapshot."""
    low, high = _PHASE_SPAN.get(snap["phase"], (0, 0))
    fraction = snap["done"] / snap["total"] if snap["total"] else 0
    return round(low + (high - low) * min(fraction, 1))


def get_job(job_id):
    with _jobs_lock:
        return _jobs.get(job_id)


def active_jobs():
    with _jobs_lock:
        return [job for job in _jobs.values() if job.active]


# ---------------------------------------------------------------------------
# The work
# ---------------------------------------------------------------------------

def _spotify_get(url, params=None, attempts=4):
    """GET from the Web API, riding out rate limits and blips.

    The old importer read `.json()` off whatever came back, so a 429 or a 5xx
    looked like an album with no tracks and those songs were silently missing.
    Now a rate limit waits out Retry-After, a server error is retried, an
    expired token is refreshed, and anything still failing raises.
    """
    for attempt in range(attempts):
        last = attempt == attempts - 1
        headers = {"Authorization": f"Bearer {get_access_token()}"}
        try:
            res = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            if last:
                raise
            time.sleep(1 + attempt)
            continue
        if res.status_code == 401 and not last:
            get_access_token(force_refresh=True)
            continue
        if (res.status_code == 429 or res.status_code >= 500) and not last:
            time.sleep(min(float(res.headers.get("Retry-After") or 1 + attempt), 30))
            continue
        if res.status_code >= 400:
            # Spotify says why in the body, and that is what an admin needs to
            # see -- e.g. a 403 for every call when the app owner's account
            # lacks the subscription Spotify requires for Web API access.
            raise ImportFailed(f"Spotify refused the request ({res.status_code}): {_spotify_reason(res)}")
        return res.json()
    raise ImportFailed("Spotify did not answer")  # only with attempts=0: the last try raises


def _spotify_reason(res):
    try:
        return res.json()["error"]["message"]
    except Exception:
        return res.text[:200] or res.reason


def _find_artist(query):
    """(artist_id, name) for what the admin typed.

    "Name || spotify_artist_id" picks an exact artist when a search would find
    the wrong one of several with the same name. That form used to leave the
    artist name blank, so the import saved albums under "" and no songs at all;
    the name now comes from Spotify's own record for that id.
    """
    if "||" in query:
        _typed, artist_id = (part.strip() for part in query.split("||", 1))
        found = _spotify_get(f"{API}/artists/{artist_id}")
        return found["id"], found["name"]
    found = _spotify_get(f"{API}/search",
                         {"q": query, "type": "artist", "limit": 1, "market": MARKET})
    items = found.get("artists", {}).get("items", [])
    if not items:
        raise ImportFailed(f"Artist '{query}' not found on Spotify.")
    return items[0]["id"], items[0]["name"]


def _list_releases(job, artist_id, group):
    """Every release in one group (album or single), paged."""
    releases, offset = [], 0
    while True:
        page = _spotify_get(f"{API}/artists/{artist_id}/albums",
                            {"include_groups": group, "market": MARKET,
                             "limit": 50, "offset": offset})
        items = page.get("items", [])
        releases.extend(items)
        job._advance(len(items))
        if len(items) < 50:
            return releases
        offset += 50


def _release_tracks(album_id):
    tracks, offset = [], 0
    while True:
        page = _spotify_get(f"{API}/albums/{album_id}/tracks", {"limit": 50, "offset": offset})
        items = page.get("items", [])
        tracks.extend(items)
        if len(items) < 50:
            return tracks
        offset += 50


def _run(job):
    try:
        job._set_phase("finding", total=1)
        artist_id, name = _find_artist(job.query)
        with job._lock:
            job.artist = name
        job._advance()

        job._set_phase("albums")
        releases = []
        for group in GROUPS:
            found = [r for r in _list_releases(job, artist_id, group)
                     if not any(word in r["name"].lower() for word in SKIPPED_WORDS)]
            with job._lock:
                job.releases[group] = len(found)
            releases += [(group, r) for r in found]

        job._set_phase("tracks", total=len(releases))
        albums, tracks, skipped = [], [], 0
        with ThreadPoolExecutor(max_workers=TRACK_WORKERS) as pool:
            futures = {pool.submit(_release_tracks, r["id"]): (group, r) for group, r in releases}
            for future in as_completed(futures):
                group, release = futures[future]
                try:
                    listing = future.result()
                except Exception as exc:
                    # One release failing costs that release, not the import.
                    # It is left out entirely -- no album row without songs --
                    # and re-running the import later fills it in.
                    skipped += 1
                    job._error(f"skipped {release['name']}: {exc}")
                    job._advance()
                    continue
                # Only tracks the artist is credited on; matched by id, since
                # two artists can share a name.
                own = [t for t in listing if any(a.get("id") == artist_id for a in t.get("artists", []))]
                images = release.get("images") or [{}]
                albums.append((release["id"], release["name"], images[0].get("url")))
                tracks += [(t["id"], t["name"], release["id"], release["name"]) for t in own]
                with job._lock:
                    job.tracks[group] += len(own)
                job._advance()

        job._set_phase("saving", total=1)
        saved = db.save_artist_catalog(name, artist_id, albums, tracks)
        job._advance()
        if saved["unmatched"]:
            job._error(f"{saved['unmatched']} track(s) could not be linked to a song")

        snap = job.snapshot()
        result = {**saved, "releases": sum(snap["releases"].values()) - skipped,
                  "tracks": len(tracks), "skipped": skipped}
        job._finish("done", result)
        event(log, "import done", "%s   %d releases, %d tracks (%d new songs) in %.1fs",
              name, result["releases"], result["tracks"], saved["new_songs"],
              time.time() - job.started_at)
    except ImportFailed as exc:
        job._finish("failed", error=str(exc))
        event(log, "import failed", "%s   %s", job.query, exc, level=logging.WARNING)
    except Exception as exc:
        log.exception("import of %s crashed", job.query)
        job._finish("failed", error=f"{type(exc).__name__}: {exc}")
