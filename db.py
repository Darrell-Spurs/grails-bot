import contextlib
import datetime as _datetime
import os
import re
import sqlite3
import threading

from utils import economy, odds

# Python 3.12 deprecated sqlite3's implicit datetime adapter. Postgres takes a
# datetime natively; SQLite is told explicitly to store the same ISO text that
# _as_naive_utc() reads back, so both backends round-trip identically.
sqlite3.register_adapter(_datetime.datetime, lambda v: v.isoformat(sep=" "))
from utils.aesthetics import RARITY_ORDER as RARITY_ORDER_SQL, UNASSIGNED

# ---------------------------------------------------------------------------
# Backend selection
#
# Local development uses SQLite (music.db). A deployment sets DATABASE_URL to a
# Postgres connection string (e.g. Supabase) and everything routes there instead.
#
# Every query in this module is written in SQLite flavour ("?" placeholders,
# COLLATE NOCASE, INSERT OR IGNORE). When running on Postgres, _translate_sql()
# rewrites each statement at execute() time, so the ~66 query functions below
# are identical for both backends and only this header is backend-aware.
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

# Absolute path so the bot works no matter which directory it's launched from
# (hosts often start the process from a different cwd than the project root).
_DEFAULT_SQLITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music.db")
DB_FILE = os.environ.get("SQLITE_PATH", _DEFAULT_SQLITE)

# Supabase caps concurrent clients, and this module opens a connection per call,
# so Postgres access goes through a pool instead of connecting each time.
PG_POOL_MIN = int(os.environ.get("PG_POOL_MIN", "1"))
PG_POOL_MAX = int(os.environ.get("PG_POOL_MAX", "10"))

_pg_pool = None
_pg_pool_lock = threading.Lock()


def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        with _pg_pool_lock:
            if _pg_pool is None:
                from psycopg_pool import ConnectionPool
                _pg_pool = ConnectionPool(
                    conninfo=DATABASE_URL,
                    min_size=PG_POOL_MIN,
                    max_size=PG_POOL_MAX,
                    # psycopg3 starts issuing server-side prepared statements after a
                    # query repeats a few times. PgBouncer in *transaction* mode (the
                    # Supabase pooler on port 6543) can hand the next statement to a
                    # different backend, which then errors on the unknown prepared
                    # statement. Disabling preparation keeps us correct on either
                    # pooler mode, at a negligible cost for queries this small.
                    kwargs={"prepare_threshold": None},
                    open=True,
                )
    return _pg_pool


# ---- SQLite -> Postgres statement translation --------------------------------

_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_COLLATE_NOCASE_RE = re.compile(r"\s+COLLATE\s+NOCASE", re.IGNORECASE)
_INSERT_OR_IGNORE_RE = re.compile(r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)


def _translate_sql(sql, has_params=False):
    """Rewrite a SQLite statement for Postgres.

    Returns None for statements that have no Postgres equivalent (PRAGMA), which
    the cursor wrapper then skips. String literals are left untouched so a '?' or
    the word COLLATE inside quoted text is never rewritten.

    has_params says whether values will be bound to this statement. psycopg only
    parses %-placeholders when parameters are supplied, so a literal % in the SQL
    (e.g. LIKE '%foo%') has to be doubled in that case -- otherwise psycopg reads
    '%f' as a bad placeholder and raises. Escaping happens before '?' becomes
    '%s' so the placeholders we introduce are left alone.
    """
    if sql.lstrip().upper().startswith("PRAGMA"):
        return None

    if has_params:
        sql = sql.replace("%", "%%")

    # Case-insensitive comparison is handled by CITEXT columns in the Postgres
    # schema, so the explicit collation just gets dropped.
    def _rewrite_outside_literals(text):
        text = _COLLATE_NOCASE_RE.sub("", text)
        return text.replace("?", "%s")

    pieces = []
    last = 0
    for m in _STRING_LITERAL_RE.finditer(sql):
        pieces.append(_rewrite_outside_literals(sql[last:m.start()]))
        pieces.append(m.group(0))
        last = m.end()
    pieces.append(_rewrite_outside_literals(sql[last:]))
    sql = "".join(pieces)

    if _INSERT_OR_IGNORE_RE.search(sql):
        sql = _INSERT_OR_IGNORE_RE.sub(r"\1INSERT INTO", sql)
        if "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    return sql


class _CompatCursor:
    """Cursor wrapper that translates SQLite SQL before handing it to Postgres.

    Mirrors the parts of the sqlite3 cursor API this module relies on, including
    execute() returning the cursor so `c.execute(...).fetchone()` keeps working.
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        translated = _translate_sql(sql, has_params=bool(params))
        if translated is None:  # PRAGMA and friends: no-op on Postgres
            return self
        if params:
            self._raw.execute(translated, tuple(params))
        else:
            # Passing an empty tuple would still make psycopg scan for
            # placeholders, so parameterless SQL is sent as-is.
            self._raw.execute(translated)
        return self

    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()

    @property
    def rowcount(self):
        return self._raw.rowcount

    def close(self):
        self._raw.close()


class _PooledConnection:
    """Postgres connection that behaves like a sqlite3 connection.

    Critically, close() returns the connection to the pool rather than tearing it
    down, so the existing "open a connection per function call" style in this
    module stays correct without exhausting Supabase's client limit.
    """

    def __init__(self, pool, raw):
        self._pool = pool
        self._raw = raw
        self._released = False

    def cursor(self):
        return _CompatCursor(self._raw.cursor())

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        if self._released:
            return
        self._released = True
        try:
            # Drop any transaction left open by an error path before reuse.
            self._raw.rollback()
        except Exception:
            pass
        self._pool.putconn(self._raw)

    def __del__(self):
        # Safety net: a function that raises before reaching close() would
        # otherwise leak a pooled connection for the life of the process.
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared connection
#
# Every query function here opens and closes its own connection. Against a
# remote pooler that costs a round-trip each time (~165ms measured, vs ~86ms
# when a connection is reused), so a command issuing several queries pays it
# over and over. shared_connection() pins one connection to the current thread
# for the duration of a block; get_connection() then hands out a non-closing
# view of it, so none of the existing functions need to change.
#
# Thread-local rather than global: the bot offloads DB work with
# asyncio.to_thread, and a SQLite connection may not cross threads anyway.
# ---------------------------------------------------------------------------

_conn_local = threading.local()


class _SharedConnection:
    """A borrowed connection. close() is a no-op -- the surrounding
    shared_connection() block owns the real lifetime."""

    def __init__(self, inner):
        self._inner = inner

    def cursor(self):
        return self._inner.cursor()

    def execute(self, sql, params=()):
        return self._inner.execute(sql, params)

    def commit(self):
        self._inner.commit()

    def rollback(self):
        self._inner.rollback()

    def close(self):
        pass


@contextlib.contextmanager
def shared_connection():
    """Run a block of queries over one connection.

        with db.shared_connection():
            a = db.get_random_song()
            b = db.get_song_rarity(...)

    Nesting is safe: an inner block reuses the outer one's connection and
    leaves closing to it.
    """
    existing = getattr(_conn_local, "conn", None)
    if existing is not None:
        yield existing
        return

    conn = _open_connection()
    _conn_local.conn = conn
    try:
        yield conn
    finally:
        _conn_local.conn = None
        conn.close()


def get_connection():
    shared = getattr(_conn_local, "conn", None)
    if shared is not None:
        return _SharedConnection(shared)
    return _open_connection()


def _open_connection():
    if IS_POSTGRES:
        pool = _get_pg_pool()
        return _PooledConnection(pool, pool.getconn())

    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite needs this per-connection to enforce FKs
    return conn

_POSTGRES_SCHEMA = (
    # CITEXT gives case-insensitive equality/LIKE on the columns the bot looks up
    # by name, which is what COLLATE NOCASE provides on SQLite.
    "CREATE EXTENSION IF NOT EXISTS citext",

    """CREATE TABLE IF NOT EXISTS songs (
        id TEXT PRIMARY KEY,
        name CITEXT,
        artist CITEXT,
        rarity TEXT
    )""",

    # SQLite's UNIQUE(name, artist) compares case-sensitively. On CITEXT columns
    # a plain UNIQUE(name, artist) would compare case-insensitively and reject
    # rows SQLite accepts (e.g. "Make You Mine" and "make you mine" by the same
    # artist are two distinct tracks here). Expressing the uniqueness over the
    # text casts keeps both backends behaving identically, while the columns
    # stay CITEXT so lookups remain case-insensitive.
    "ALTER TABLE songs DROP CONSTRAINT IF EXISTS songs_name_artist_key",
    """CREATE UNIQUE INDEX IF NOT EXISTS songs_name_artist_unique
       ON songs ((name::text), (artist::text))""",

    """CREATE TABLE IF NOT EXISTS albums (
        id TEXT PRIMARY KEY,
        name CITEXT,
        artist CITEXT,
        artist_id TEXT,
        image TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS song_album (
        song_id TEXT,
        spotify_song_id TEXT PRIMARY KEY,
        album_name TEXT,
        album_id TEXT,
        FOREIGN KEY (song_id) REFERENCES songs(id),
        FOREIGN KEY (album_id) REFERENCES albums(id)
    )""",

    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT,
        xp INTEGER DEFAULT 0,
        last_daily_claim TIMESTAMP DEFAULT NULL,
        vinyl_count INTEGER DEFAULT 0,
        sig_vinyl_count INTEGER DEFAULT 0,
        pinned_collection_id INTEGER DEFAULT NULL,
        favorite_artist TEXT DEFAULT NULL,
        pull_charges INTEGER DEFAULT 20,
        pull_charges_at TIMESTAMP DEFAULT NULL
    )""",

    # Existing deployments predate the profile columns. Postgres supports
    # IF NOT EXISTS on ADD COLUMN, so these are safe to run on every start.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS pinned_collection_id INTEGER DEFAULT NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS favorite_artist TEXT DEFAULT NULL",

    # Drop economy. pull_charges_at is the anchor the balance regenerates from;
    # NULL means "never spent", which utils/economy.py reads as a full stack, so
    # players who predate the feature are not punished for it.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS pull_charges INTEGER DEFAULT 20",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS pull_charges_at TIMESTAMP DEFAULT NULL",

    """CREATE TABLE IF NOT EXISTS collections (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id TEXT,
        song_id TEXT,
        album_id TEXT,
        variant TEXT DEFAULT 'default',
        collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (song_id) REFERENCES songs(id),
        FOREIGN KEY (album_id) REFERENCES albums(id)
    )""",

    """CREATE TABLE IF NOT EXISTS mythic_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        song_id TEXT,
        song_name TEXT,
        album_id TEXT,
        album_name TEXT,
        album_url TEXT,
        artist TEXT
    )""",

    "CREATE INDEX IF NOT EXISTS idx_user ON collections(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_song ON collections(song_id)",
    "CREATE INDEX IF NOT EXISTS idx_collections_user_variant ON collections(user_id, variant)",
    "CREATE INDEX IF NOT EXISTS idx_collections_song_variant ON collections(song_id, variant)",
    "CREATE INDEX IF NOT EXISTS idx_songs_artist_rarity ON songs(artist, rarity)",
    "CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist)",
    "CREATE INDEX IF NOT EXISTS idx_song_album_song ON song_album(song_id)",
    "CREATE INDEX IF NOT EXISTS idx_song_album_album ON song_album(album_id)",
)


# ---------------------------------------------------------------------------
# One-time data migration, run on every start because it is idempotent.
#
# "common" used to be the rarity a freshly imported song was given, which made
# it read like a sixth tier in every listing even though the draw tables never
# name it and it can never be pulled. It is now spelled "unassigned", which is
# what it always meant. This also sweeps up any other value that is not a tier,
# so a stray rarity shows as unassigned instead of rendering as a grey unknown
# for the rest of time.
#
# The values are interpolated rather than bound because they come from our own
# palette, and the statement has to run identically on both backends -- which
# do not share a placeholder style.
# ---------------------------------------------------------------------------
_KNOWN_RARITIES_SQL = ", ".join(
    "'" + r + "'" for r in [*RARITY_ORDER_SQL, UNASSIGNED])

MIGRATE_RARITIES_SQL = (
    "UPDATE songs SET rarity = '" + UNASSIGNED + "' "
    "WHERE rarity IS NULL OR LOWER(rarity) NOT IN (" + _KNOWN_RARITIES_SQL + ")"
)


def _init_postgres():
    """Create the schema on Postgres. Mirrors the SQLite schema below, with
    AUTOINCREMENT -> IDENTITY and the name columns typed CITEXT."""
    conn = get_connection()
    try:
        c = conn.cursor()
        for statement in _POSTGRES_SCHEMA:
            c.execute(statement)
        c.execute(MIGRATE_RARITIES_SQL)
        conn.commit()
    finally:
        conn.close()


def init_db():
    if IS_POSTGRES:
        _init_postgres()
        return

    conn = get_connection()
    c = conn.cursor()

    c.execute("PRAGMA journal_mode=WAL")

    c.execute("""
    CREATE TABLE IF NOT EXISTS songs (
        id TEXT PRIMARY KEY,
        name TEXT,
        artist TEXT,
        rarity TEXT,
        UNIQUE(name, artist)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS albums (
        id TEXT PRIMARY KEY,
        name TEXT,
        artist TEXT,
        artist_id TEXT,
        image TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS song_album (
        song_id TEXT,
        spotify_song_id TEXT,
        album_name TEXT,
        album_id TEXT,
        PRIMARY KEY (spotify_song_id),
        FOREIGN KEY (song_id) REFERENCES songs(id),
        FOREIGN KEY (album_id) REFERENCES albums(id)
    )
    """)

    # NOTE: the whole codebase reads/writes users.id (not user_id). This declaration
    # now matches the code so a fresh database works identically to the live one.
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT,
        xp INTEGER DEFAULT 0,
        last_daily_claim TIMESTAMP DEFAULT NULL,
        vinyl_count INTEGER DEFAULT 0,
        sig_vinyl_count INTEGER DEFAULT 0,
        pinned_collection_id INTEGER DEFAULT NULL,
        favorite_artist TEXT DEFAULT NULL,
        pull_charges INTEGER DEFAULT 20,
        pull_charges_at TIMESTAMP DEFAULT NULL
    )
    """)

    c.execute("""
       CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            song_id TEXT,
            album_id TEXT, 
            variant TEXT DEFAULT 'default', 
            collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP , 
            FOREIGN KEY (song_id) REFERENCES songs(id), 
            FOREIGN KEY (album_id) REFERENCES albums(id)
        );
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_user ON collections(user_id);
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_song ON collections(song_id);
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS mythic_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        song_id TEXT,
        song_name TEXT,
        album_id TEXT,
        album_name TEXT,
        album_url TEXT,
        artist TEXT
    )
    """)

    # ---- one-time migration: add any columns missing on an older `users` table ----
    c.execute("PRAGMA table_info(users)")
    user_cols = {row[1] for row in c.fetchall()}
    for col, ddl in (
        ("username", "ALTER TABLE users ADD COLUMN username TEXT"),
        ("xp", "ALTER TABLE users ADD COLUMN xp INTEGER DEFAULT 0"),
        ("last_daily_claim", "ALTER TABLE users ADD COLUMN last_daily_claim TIMESTAMP DEFAULT NULL"),
        ("vinyl_count", "ALTER TABLE users ADD COLUMN vinyl_count INTEGER DEFAULT 0"),
        ("sig_vinyl_count", "ALTER TABLE users ADD COLUMN sig_vinyl_count INTEGER DEFAULT 0"),
        ("pinned_collection_id", "ALTER TABLE users ADD COLUMN pinned_collection_id INTEGER DEFAULT NULL"),
        ("favorite_artist", "ALTER TABLE users ADD COLUMN favorite_artist TEXT DEFAULT NULL"),
        ("pull_charges", "ALTER TABLE users ADD COLUMN pull_charges INTEGER DEFAULT 20"),
        ("pull_charges_at", "ALTER TABLE users ADD COLUMN pull_charges_at TIMESTAMP DEFAULT NULL"),
    ):
        if col not in user_cols:
            c.execute(ddl)

    # ---- indexes for the common hot-path filters ----
    c.execute("CREATE INDEX IF NOT EXISTS idx_collections_user_variant ON collections(user_id, variant)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_collections_song_variant ON collections(song_id, variant)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_songs_artist_rarity ON songs(artist, rarity)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_song_album_song ON song_album(song_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_song_album_album ON song_album(album_id)")

    c.execute(MIGRATE_RARITIES_SQL)

    conn.commit()
    conn.close()

def alter_users_table():
    """Add xp and last_daily_claim columns to existing users table if they don't exist"""
    conn = get_connection()
    c = conn.cursor()
    
    try:
        # Check what columns already exist
        c.execute("PRAGMA table_info(users)")
        columns = [column[1] for column in c.fetchall()]
        
        if 'xp' not in columns:
            c.execute("ALTER TABLE users ADD COLUMN xp INTEGER DEFAULT 0")
            print("Added xp column to users table")
        else:
            print("xp column already exists in users table")
            
        if 'last_daily_claim' not in columns:
            c.execute("ALTER TABLE users ADD COLUMN last_daily_claim TIMESTAMP DEFAULT NULL")
            print("Added last_daily_claim column to users table")
        else:
            print("last_daily_claim column already exists in users table")
            
        conn.commit()
    except Exception as e:
        print(f"Error altering users table: {e}")
        conn.rollback()
    finally:
        conn.close()

def _uid(user_id):
    """Normalise a Discord user id to the TEXT form the id columns use.

    discord.py hands us ints (ctx.author.id), the web panel hands us strings
    (URL path segments), and the users.id / collections.user_id columns are
    TEXT on both backends. SQLite coerced int->text silently, so the mismatch
    never showed; Postgres is strictly typed and raises

        UndefinedFunction: operator does not exist: text = bigint

    Normalising here means callers can pass either form.
    """
    return None if user_id is None else str(user_id)


def add_song(song_id, name, artist, rarity=UNASSIGNED):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO songs (id, name, artist, rarity) VALUES (?, ?, ?, ?)", (song_id, name, artist, rarity))
    
    song_id = None
    if c.rowcount == 0:
        # Insert was ignored → fetch existing song_id
        c.execute("SELECT id FROM songs WHERE name = ? AND artist = ?", (name, artist))
        existing = c.fetchone()
        song_id = existing[0]
    conn.commit()
    conn.close()

    return song_id

def add_album(album_id, name, artist, artist_id, image):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO albums (id, name, artist, artist_id, image) VALUES (?, ?, ?, ?, ?)", (album_id, name, artist, artist_id, image))
    conn.commit()
    conn.close()

def link_song_album(song_id, spotify_song_id, album_id, album_name):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO song_album (song_id, spotify_song_id, album_id, album_name) VALUES (?, ?, ?, ?)", (song_id, spotify_song_id, album_id, album_name))
    conn.commit()
    conn.close()

def add_song_to_user(user_id, song_id):
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO collections (user_id, song_id) VALUES (?, ?)", (user_id, song_id))
    conn.commit()
    conn.close()

def get_albums_by_artist(artist_name):
    """Get all albums by a specific artist from the database"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, name FROM albums WHERE artist = ?", (artist_name,))
    results = c.fetchall()
    conn.close()
    return results  # List of (album_id, album_name)

def get_all_artists():
    """Get all unique artists from the database"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT DISTINCT artist FROM albums ORDER BY artist")
    results = c.fetchall()
    conn.close()
    return [row[0] for row in results]  # List of artist names

def get_album_song_counts(artist):
    """[(album_name, song_count)] for one artist, fullest release first.

    Deliberately narrow: album autocomplete fires on every keystroke, and
    pulling one artist's whole song list to count albums in Python would mean
    shipping 400+ rows per character for a back catalogue like Taylor Swift's.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT albums.name, COUNT(DISTINCT song_album.song_id) AS songs
        FROM albums
        JOIN song_album ON song_album.album_id = albums.id
        WHERE albums.artist = ? COLLATE NOCASE
        GROUP BY albums.name
        ORDER BY songs DESC, albums.name ASC
    """, (artist,))
    rows = c.fetchall()
    conn.close()
    return [(name, n) for name, n in rows]


def get_artist_collected_counts():
    """{artist_lower: total copies collected server-wide}.

    Counts collection rows, not distinct songs: an artist whose songs have been
    pulled fifty times between them outranks one with more songs that nobody
    owns, which is what "most collected" means to a player.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT songs.artist, COUNT(*)
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        GROUP BY songs.artist
    """)
    rows = c.fetchall()
    conn.close()
    return {(a or "").lower(): n for a, n in rows}


def get_artist_favourite_counts():
    """{artist_lower: how many players have set them as their favourite}."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT favorite_artist, COUNT(*)
        FROM users
        WHERE favorite_artist IS NOT NULL AND favorite_artist <> ''
        GROUP BY favorite_artist
    """)
    rows = c.fetchall()
    conn.close()
    return {(a or "").lower(): n for a, n in rows}


def get_artist_overview():
    """Every artist with their album/song counts and up to four cover images.

    Two queries rather than one: pulling "N covers per group" in a single
    statement needs a window function or LATERAL, and the portable spellings
    differ between SQLite and Postgres. The albums table is small enough that
    fetching its (artist, image) pairs and grouping here is cheaper than the
    complexity, and it keeps the catalog page at 2 queries instead of 1-per-artist.
    """
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT albums.artist,
               COUNT(DISTINCT albums.id) AS album_count,
               COUNT(DISTINCT song_album.song_id) AS song_count
        FROM albums
        LEFT JOIN song_album ON song_album.album_id = albums.id
        GROUP BY albums.artist
        ORDER BY albums.artist
    """)
    counts = c.fetchall()

    c.execute("""
        SELECT artist, image FROM albums
        WHERE image IS NOT NULL AND image <> ''
        ORDER BY artist, name
    """)
    covers = {}
    for artist, image in c.fetchall():
        bucket = covers.setdefault(artist, [])
        if len(bucket) < 4 and image not in bucket:
            bucket.append(image)

    conn.close()

    return [
        {
            "artist": artist,
            "album_count": album_count,
            "song_count": song_count,
            "covers": covers.get(artist, []),
        }
        for artist, album_count, song_count in counts
    ]

def get_artist_collection_stats(artist_name):
    """How much of one artist's catalog players actually hold.

    One aggregate over collections: total copies out there, how many distinct
    songs have ever been pulled, how many players own something, and how many
    of those copies are mythics.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*),
               COUNT(DISTINCT collections.song_id),
               COUNT(DISTINCT collections.user_id),
               SUM(CASE WHEN collections.variant = 'mythic' THEN 1 ELSE 0 END)
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        WHERE songs.artist = ? COLLATE NOCASE
    """, (artist_name,))
    row = c.fetchone()
    conn.close()

    # COUNT gives 0 with no rows, but SUM(CASE ...) gives NULL.
    copies, distinct_songs, collectors, mythics = row if row else (0, 0, 0, 0)
    return {
        "copies": copies or 0,
        "songs_collected": distinct_songs or 0,
        "collectors": collectors or 0,
        "mythics": mythics or 0,
    }

def get_artist_popularity(artist_name):
    """Where this artist ranks against every other by total copies pulled.

    Standard competition ranking: artists tied on copies share a rank. Artists
    with no pulls at all still rank -- they simply tie for last -- so the
    denominator is every artist in the catalog, not just the ones with pulls.
    """
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT songs.artist, COUNT(*)
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        GROUP BY songs.artist
    """)
    pulls = {row[0]: row[1] for row in c.fetchall()}

    c.execute("SELECT DISTINCT artist FROM albums")
    artists = [row[0] for row in c.fetchall()]
    conn.close()

    if not artists:
        return {"rank": None, "total": 0, "copies": 0}

    # Names come from two tables, so compare case-insensitively to keep an
    # artist from being counted as two.
    lowered = {a.lower(): pulls.get(a, 0) for a in artists}
    for name, count in pulls.items():
        key = name.lower()
        if key in lowered:
            lowered[key] = count

    mine = lowered.get(artist_name.lower(), 0)
    rank = 1 + sum(1 for count in lowered.values() if count > mine)
    return {"rank": rank, "total": len(lowered), "copies": mine}

def get_song_by_artist(artist_name):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT name, id FROM songs WHERE artist = ?", (artist_name,))
    results = c.fetchall()
    return results  # List of (song_name,)

def get_album_details_by_song(song_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT albums.id, albums.name, albums.image
        FROM song_album
        JOIN albums ON song_album.album_id = albums.id
        WHERE song_album.song_id = ?
    """, (song_id,))
    result = c.fetchall()
    conn.close()
    return result 

def add_song_to_collection(user_id, song_id, album_id, variant='default'):
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO collections (user_id, song_id, album_id, variant)
        VALUES (?, ?, ?, ?)
    """, (user_id, song_id, album_id, variant))
    conn.commit()
    conn.close()

def get_collection_by_user(user_id):
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT songs.name, songs.artist, collections.variant, albums.name, albums.image
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ?
        ORDER BY
            CASE collections.variant
                WHEN 'mythic' THEN 1
                WHEN 'sig_vinyl' THEN 2
                WHEN 'vinyl' THEN 3
                WHEN 'sketch' THEN 4
                WHEN 'glitched' THEN 5
                WHEN 'default' THEN 6
                ELSE 7
            END,
            songs.artist ASC,
            songs.name ASC,
            albums.name ASC
    """, (user_id,))
    results = c.fetchall()
    conn.close()
    return results 

def get_collection_by_artist(user_id, artist_name):
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT songs.name, songs.artist, collections.variant, albums.name, albums.image
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ? AND songs.artist = ? COLLATE NOCASE
        ORDER BY CASE collections.variant
            WHEN 'mythic' THEN 1
            WHEN 'sig_vinyl' THEN 2
            WHEN 'vinyl' THEN 3
            WHEN 'sketch' THEN 4
            WHEN 'glitched' THEN 5
            WHEN 'default' THEN 6
            ELSE 7
        END, songs.artist ASC
    """, (user_id, artist_name))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_name, artist_name, variant, album_name, album_image)

def get_collection_by_rarity(user_id, rarity):
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT songs.name, songs.artist, collections.variant, albums.name, albums.image
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ? AND collections.variant = ?
        ORDER BY CASE collections.variant
            WHEN 'mythic' THEN 1
            WHEN 'sig_vinyl' THEN 2
            WHEN 'vinyl' THEN 3
            WHEN 'sketch' THEN 4
            WHEN 'glitched' THEN 5
            WHEN 'default' THEN 6
            ELSE 7
        END, songs.artist ASC
    """, (user_id, rarity))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_name, artist_name, variant, album_name, album_image)

def get_collection_by_rarity_and_artist(user_id, rarity, artist_name):
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT songs.name, songs.artist, collections.variant, albums.name, albums.image
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ? AND collections.variant = ? AND songs.artist = ? COLLATE NOCASE
        ORDER BY CASE collections.variant
            WHEN 'mythic' THEN 1
            WHEN 'sig_vinyl' THEN 2
            WHEN 'vinyl' THEN 3
            WHEN 'sketch' THEN 4
            WHEN 'glitched' THEN 5
            WHEN 'default' THEN 6
            ELSE 7
        END, songs.artist ASC
    """, (user_id, rarity, artist_name))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_name, artist_name, variant, album_name, album_image)

def get_collection_by_rarity_with_copy_numbers(user_id, rarity):
    """Get user's collection filtered by rarity, including copy numbers for mythic items"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT 
            songs.name, 
            songs.artist, 
            collections.variant, 
            albums.name, 
            albums.image,
            CASE 
                WHEN collections.variant = 'mythic' THEN
                    (SELECT COUNT(*) + 1
                     FROM collections c2
                     WHERE c2.song_id = collections.song_id 
                     AND c2.variant = 'mythic' 
                     AND c2.collected_at < collections.collected_at)
                ELSE 1
            END as copy_number,
            songs.rarity,
            collections.id
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ? AND collections.variant = ?
        ORDER BY
            CASE songs.rarity
                WHEN 'ultimate' THEN 1
                WHEN 'legendary' THEN 2
                WHEN 'elite' THEN 3
                WHEN 'unique' THEN 4
                WHEN 'basic' THEN 5
                ELSE 6
            END ASC,
            songs.artist ASC,
            songs.name ASC,
            albums.name ASC
    """, (user_id, rarity))
    results = c.fetchall()
    conn.close()
    return results

def get_collection_by_artist_with_copy_numbers(user_id, artist_name):
    """Get user's collection filtered by artist, including copy numbers for mythic items"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT 
            songs.name, 
            songs.artist, 
            collections.variant, 
            albums.name, 
            albums.image,
            CASE 
                WHEN collections.variant = 'mythic' THEN
                    (SELECT COUNT(*) + 1
                     FROM collections c2
                     WHERE c2.song_id = collections.song_id 
                     AND c2.variant = 'mythic' 
                     AND c2.collected_at < collections.collected_at)
                ELSE 1
            END as copy_number,
            songs.rarity,
            collections.id
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ? AND LOWER(songs.artist) LIKE LOWER(?)
        ORDER BY
            CASE songs.rarity
                WHEN 'ultimate' THEN 1
                WHEN 'legendary' THEN 2
                WHEN 'elite' THEN 3
                WHEN 'unique' THEN 4
                WHEN 'basic' THEN 5
                ELSE 6
            END ASC,
            songs.artist ASC,
            songs.name ASC,
            albums.name ASC
    """, (user_id, f"%{artist_name}%"))
    results = c.fetchall()
    conn.close()
    return results

def get_collection_by_rarity_and_artist_with_copy_numbers(user_id, rarity, artist_name):
    """Get user's collection filtered by rarity and artist, including copy numbers for mythic items"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT 
            songs.name, 
            songs.artist, 
            collections.variant, 
            albums.name, 
            albums.image,
            CASE 
                WHEN collections.variant = 'mythic' THEN
                    (SELECT COUNT(*) + 1
                     FROM collections c2
                     WHERE c2.song_id = collections.song_id 
                     AND c2.variant = 'mythic' 
                     AND c2.collected_at < collections.collected_at)
                ELSE 1
            END as copy_number,
            songs.rarity,
            collections.id
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ? AND collections.variant = ? AND LOWER(songs.artist) LIKE LOWER(?)
        ORDER BY
            CASE songs.rarity
                WHEN 'ultimate' THEN 1
                WHEN 'legendary' THEN 2
                WHEN 'elite' THEN 3
                WHEN 'unique' THEN 4
                WHEN 'basic' THEN 5
                ELSE 6
            END ASC,
            songs.artist ASC,
            songs.name ASC,
            albums.name ASC
    """, (user_id, rarity, f"%{artist_name}%"))
    results = c.fetchall()
    conn.close()
    return results

def remove_mythic_from_collection():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        DELETE FROM collections
        WHERE variant = 'mythic'
    """)
    conn.commit()
    conn.close()

def get_user_song_by_details(user_id, rarity, song_name, artist_name, album_name):
    """Check if user has a specific song with the given variant.

    Trailing `songs.rarity` lets callers render the combined rarity+variant
    card emoji without a second lookup."""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT collections.id, songs.id as song_id, albums.id as album_id, songs.name, songs.artist, collections.variant, albums.name as album_name, songs.rarity
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ? AND collections.variant = ? 
        AND LOWER(songs.name) = LOWER(?) AND LOWER(songs.artist) = LOWER(?)
        LIMIT 1
    """, (user_id, rarity, song_name, artist_name))
    result = c.fetchone()
    conn.close()
    return result

def get_user_tradeable_items(user_id, rarity, artist=None):
    """Get a user's collection rows for a given rarity (optionally narrowed to one artist),
    for trade selection/autocomplete. Same column order as get_user_song_by_details:
    (collection_id, song_id, album_id, song_name, artist_name, variant, album_name, rarity)."""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    if artist:
        c.execute("""
            SELECT collections.id, songs.id, albums.id, songs.name, songs.artist, collections.variant, albums.name, songs.rarity
            FROM collections
            JOIN songs ON collections.song_id = songs.id
            JOIN albums ON collections.album_id = albums.id
            WHERE collections.user_id = ? AND collections.variant = ? AND songs.artist = ? COLLATE NOCASE
            ORDER BY songs.name ASC
        """, (user_id, rarity, artist))
    else:
        c.execute("""
            SELECT collections.id, songs.id, albums.id, songs.name, songs.artist, collections.variant, albums.name, songs.rarity
            FROM collections
            JOIN songs ON collections.song_id = songs.id
            JOIN albums ON collections.album_id = albums.id
            WHERE collections.user_id = ? AND collections.variant = ?
            ORDER BY songs.artist ASC, songs.name ASC
        """, (user_id, rarity))
    results = c.fetchall()
    conn.close()
    return results

def get_collection_item_by_id(collection_id, user_id):
    """Get a single collection row by its id, scoped to a user (ownership check).
    Same column order as get_user_song_by_details."""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT collections.id, songs.id, albums.id, songs.name, songs.artist, collections.variant, albums.name, songs.rarity
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.id = ? AND collections.user_id = ?
    """, (collection_id, user_id))
    result = c.fetchone()
    conn.close()
    return result

def transfer_collection_item(collection_id, from_user_id, to_user_id):
    """Move one card between players. True if it moved, False if it was not theirs.

    Reassigns the existing row rather than deleting and re-inserting it. The row
    id and collected_at are what decide a mythic's copy number, so a
    delete-then-insert would silently renumber it -- copy #1 could become #3
    just by being gifted.

    The WHERE clause carries the old owner, so a stale id cannot move somebody
    else's card.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE collections SET user_id = ?
        WHERE id = ? AND user_id = ?
    """, (_uid(to_user_id), collection_id, _uid(from_user_id)))
    moved = c.rowcount > 0
    conn.commit()
    conn.close()
    return moved


def remove_from_collection(user_id, song_id, album_id, variant):
    """Remove a specific item from user's collection"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
    DELETE FROM collections
    WHERE rowid = (
        SELECT rowid FROM collections
        WHERE user_id = ? AND song_id = ? AND album_id = ? AND variant = ?
        LIMIT 1
    )
    """, (user_id, song_id, album_id, variant))
    conn.commit()
    conn.close()
    
def register_user(user_id, xp=0, username=None):
    """Register a new user in the database"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (id, xp, username) VALUES (?, ?, ?)", (user_id, xp, username))
    conn.commit()
    conn.close()

def get_song_and_album_details(song_name, album_name, artist):
    """Get song_id, album_id, and album_url by song name, album name, and artist"""
    conn = get_connection()
    c = conn.cursor()
    
    # Find the song ID (case-insensitive search)
    c.execute("SELECT id, name FROM songs WHERE name = ? COLLATE NOCASE AND artist = ? COLLATE NOCASE", (song_name, artist))
    song_result = c.fetchone()
    
    if not song_result:
        conn.close()
        return None, None, None, None, None  # Song not found
    
    song_id = song_result[0]
    song_name = song_result[1]
    
    # Find the album ID and image URL (case-insensitive search)
    c.execute("SELECT id, name, image FROM albums WHERE name = ? COLLATE NOCASE AND artist = ? COLLATE NOCASE", (album_name, artist))
    album_result = c.fetchone()
    
    if not album_result:
        conn.close()
        return song_id, song_name, None, None, None  # Album not found

    album_id, album_name, album_url = album_result

    conn.close()
    print(f"Found song_id: {song_id}, album_id: {album_id}, album_url: {album_url}")
    return song_id, song_name, album_id, album_name, album_url

def get_songs_by_artist_and_album_category(artist_name, album_name=None):
    """
    Get songs by artist and album category.
    If album_name is "singles", returns songs from albums categorized as singles (3 or fewer tracks).
    If album_name is provided, returns songs from that specific album.
    Album categories:
    - 8+ songs: album
    - 4-7 songs: EP  
    - 3 or less: single
    """
    conn = get_connection()
    c = conn.cursor()
    
    if album_name == "singles":
        # Get songs from albums with 3 or fewer tracks (singles)
        # Exclude songs that also appear on albums/EPs (4+ tracks)
        c.execute("""
            SELECT songs.id, songs.name, albums.name as album_name, 
                   COUNT(song_album.song_id) OVER (PARTITION BY albums.id) as track_count
            FROM songs
            JOIN song_album ON songs.id = song_album.song_id
            JOIN albums ON song_album.album_id = albums.id
            WHERE songs.artist = ? COLLATE NOCASE
            AND songs.id NOT IN (
                SELECT sa2.song_id
                FROM song_album sa2
                WHERE sa2.album_id IN (
                    SELECT album_id
                    FROM song_album
                    GROUP BY album_id
                    HAVING COUNT(song_id) >= 4
                )
            )
            AND albums.id IN (
                SELECT album_id
                FROM song_album
                GROUP BY album_id
                HAVING COUNT(song_id) <= 3
            )
        """, (artist_name,))
    elif album_name:
        # Get songs from specific album
        c.execute("""
            SELECT songs.id, songs.name, albums.name as album_name,
                   COUNT(song_album.song_id) OVER (PARTITION BY albums.id) as track_count
            FROM songs
            JOIN song_album ON songs.id = song_album.song_id
            JOIN albums ON song_album.album_id = albums.id
            WHERE songs.artist = ? COLLATE NOCASE AND albums.name = ? COLLATE NOCASE
        """, (artist_name, album_name))
    else:
        # Get all songs by artist with album info
        c.execute("""
            SELECT songs.name, albums.name as album_name,
                   COUNT(song_album.song_id) OVER (PARTITION BY albums.id) as track_count
            FROM songs
            JOIN song_album ON songs.id = song_album.song_id
            JOIN albums ON song_album.album_id = albums.id
            WHERE songs.artist = ? COLLATE NOCASE
        """, (artist_name,))
    
    results = c.fetchall()
    conn.close()
    return results

def get_album_category(track_count):
    """Determine album category based on track count"""
    if track_count >= 8:
        return "Album"
    elif track_count >= 4:
        return "EP"
    else:
        return "Single"

def set_song_rarity(song_id, rarity):
    """Set the rarity of a song given its song_id"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE songs SET rarity = ? WHERE id = ?", (rarity, song_id))
    rows_affected = c.rowcount
    conn.commit()
    conn.close()
    return rows_affected > 0  # Returns True if song was found and updated

def get_song_rarity(song_id):
    """Get the current rarity of a song given its song_id"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT rarity FROM songs WHERE id = ?", (song_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def get_song_rarity_counts_by_artist(artist_name):
    """Get count of songs for each rarity by artist"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT rarity, COUNT(*) as count
        FROM songs
        WHERE artist = ? COLLATE NOCASE
        GROUP BY rarity
        ORDER BY 
            CASE rarity
                WHEN 'ultimate' THEN 1
                WHEN 'legendary' THEN 2
                WHEN 'elite' THEN 3
                WHEN 'unique' THEN 4
                WHEN 'basic' THEN 5
                ELSE 6
            END
    """, (artist_name,))
    results = c.fetchall()
    conn.close()
    return results  # List of (rarity, count)

def get_songs_by_artist_and_rarity(artist_name, rarity):
    """Get all songs of a specific rarity by artist"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT songs.id, songs.name, albums.name as album_name, songs.rarity
        FROM songs
        JOIN song_album ON songs.id = song_album.song_id
        JOIN albums ON song_album.album_id = albums.id
        WHERE songs.artist = ? COLLATE NOCASE AND songs.rarity = ? COLLATE NOCASE
        ORDER BY songs.name ASC
    """, (artist_name, rarity))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_id, song_name, album_name, rarity)

def remove_songs_by_artist(artist_name):
    """Remove all songs, albums, and song_album entries for a given artist"""
    conn = get_connection()
    c = conn.cursor()
    
    try:
        # Get all song IDs for this artist before deletion
        c.execute("SELECT id FROM songs WHERE artist = ? COLLATE NOCASE", (artist_name,))
        song_ids = [row[0] for row in c.fetchall()]
        
        # Get all album IDs for this artist before deletion
        c.execute("SELECT id FROM albums WHERE artist = ? COLLATE NOCASE", (artist_name,))
        album_ids = [row[0] for row in c.fetchall()]
        
        # Remove from song_album table (songs by this artist)
        if song_ids:
            placeholders = ','.join(['?' for _ in song_ids])
            c.execute(f"DELETE FROM song_album WHERE song_id IN ({placeholders})", song_ids)
            song_album_deletions = c.rowcount
            # FK enforcement is now ON, so any collections referencing these songs must
            # go before the parent songs/albums rows can be deleted.
            c.execute(f"DELETE FROM collections WHERE song_id IN ({placeholders})", song_ids)
        else:
            song_album_deletions = 0
        if album_ids:
            album_placeholders = ','.join(['?' for _ in album_ids])
            c.execute(f"DELETE FROM collections WHERE album_id IN ({album_placeholders})", album_ids)

        # Remove from songs table
        c.execute("DELETE FROM songs WHERE artist = ? COLLATE NOCASE", (artist_name,))
        song_deletions = c.rowcount
        
        # Remove from albums table
        c.execute("DELETE FROM albums WHERE artist = ? COLLATE NOCASE", (artist_name,))
        album_deletions = c.rowcount
        
        conn.commit()
        
        return {
            'songs_deleted': song_deletions,
            'albums_deleted': album_deletions,
            'song_album_links_deleted': song_album_deletions,
            'success': True
        }
        
    except Exception as e:
        conn.rollback()
        return {
            'songs_deleted': 0,
            'albums_deleted': 0,
            'song_album_links_deleted': 0,
            'success': False,
            'error': str(e)
        }
    finally:
        conn.close()

def get_user_xp(user_id):
    """Get the current XP of a user"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT xp FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def update_user_xp(user_id, xp):
    """Update the XP of a user"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET xp = ? WHERE id = ?", (xp, user_id))
    rows_affected = c.rowcount
    conn.commit()
    conn.close()
    return rows_affected > 0

def add_user_xp(user_id, xp_to_add):
    """Add XP to a user's current XP"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET xp = xp + ? WHERE id = ?", (xp_to_add, user_id))
    rows_affected = c.rowcount
    conn.commit()
    conn.close()
    return rows_affected > 0

def get_user_stats(user_id):
    """Get user's xp"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT xp FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result:
        return {
            'xp': result[0],
        }
    return None

def user_exists(user_id):
    """Check if a user exists in the database"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def get_last_daily_claim(user_id):
    """Get the last daily claim timestamp for a user"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT last_daily_claim FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def update_daily_claim(user_id):
    """Update the last daily claim timestamp for a user to now"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET last_daily_claim = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
    rows_affected = c.rowcount
    conn.commit()
    conn.close()
    return rows_affected > 0

def can_claim_daily(user_id):
    """Check if user can claim daily reward (24 hours since last claim)"""
    user_id = _uid(user_id)
    import datetime
    
    last_claim = get_last_daily_claim(user_id)
    if not last_claim:
        return True, 0  # Never claimed before
    
    # Parse the timestamp
    try:
        # Parse the SQLite timestamp as naive UTC datetime
        # SQLite stores timestamps as TEXT and hands back a string; Postgres
        # has a real TIMESTAMP type and hands back a datetime object. Calling
        # .replace('Z', '') on the latter raises TypeError, which the except
        # below would swallow into 'return True, 0' -- silently disabling the
        # 24h cooldown. Normalise both shapes to a naive UTC datetime.
        if isinstance(last_claim, datetime.datetime):
            last_claim_dt = last_claim
        else:
            last_claim_dt = datetime.datetime.fromisoformat(str(last_claim).replace('Z', ''))
        if last_claim_dt.tzinfo is not None:
            last_claim_dt = last_claim_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        
        # Get current time in UTC but make it naive (no timezone info)
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                
        time_diff = now - last_claim_dt
        
        # Check if 24 hours have passed
        if time_diff.total_seconds() >= 86400:  # 24 hours in seconds
            return True, 0
        else:
            remaining_seconds = 86400 - time_diff.total_seconds()
            return False, remaining_seconds
    except Exception as e:
        print(f"Error parsing timestamp: {e}")
        return True, 0  # Allow claim if there's an error

# ===== DROP ECONOMY =====

def _as_naive_utc(value):
    """Normalise a stored timestamp to a naive UTC datetime.

    SQLite hands back a string, Postgres a datetime, and a Postgres datetime may
    or may not carry a timezone depending on the column type. Everything below
    compares against datetime.utcnow(), so all three shapes are flattened here
    rather than at each call site.
    """
    import datetime
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        try:
            parsed = datetime.datetime.fromisoformat(str(value).replace("Z", ""))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return parsed


def _read_pull_row(c, user_id):
    """(charges, anchor) straight from the row, or None when not registered."""
    c.execute("SELECT pull_charges, pull_charges_at FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        return None
    return (row[0] if row[0] is not None else economy.PULL_CAP), _as_naive_utc(row[1])


def get_pull_status(user_id):
    """A user's drop charges, brought up to date.

    Regeneration is applied and written back, so the stored row is correct for
    anything that reads it later (the admin panel, a second command) without
    each reader having to know the rules.

    Returns None when the user is not registered, otherwise
    {charges, next_in, full_in, cap}.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    try:
        c = conn.cursor()
        state = _read_pull_row(c, user_id)
        if state is None:
            return None
        charges, anchor = state
        now = economy._utcnow()
        fresh, new_anchor = economy.regenerate(charges, anchor, now)

        if (fresh, new_anchor) != (charges, anchor):
            c.execute("UPDATE users SET pull_charges = ?, pull_charges_at = ? WHERE id = ?",
                      (fresh, new_anchor, user_id))
            conn.commit()

        return {
            "charges": fresh,
            "cap": economy.PULL_CAP,
            "next_in": economy.seconds_to_next(fresh, new_anchor, now),
            "full_in": economy.seconds_to_full(fresh, new_anchor, now),
        }
    finally:
        conn.close()


def spend_pull_charge(user_id, cost=None):
    """Take `cost` charges if they are there. Returns (spent, status).

    Regeneration is applied first, so a player who has been away always gets
    what they are owed before the cost is taken. `spent` is False when the
    balance is short, and nothing is written in that case.

    Callers must serialise their own concurrent spends (choice_cog holds a
    per-user lock): this reads and writes in two statements, which is safe for a
    single bot process but would need a conditional UPDATE across several.
    """
    user_id = _uid(user_id)
    cost = economy.PULL_COST if cost is None else cost
    conn = get_connection()
    try:
        c = conn.cursor()
        state = _read_pull_row(c, user_id)
        if state is None:
            return False, None
        charges, anchor = state
        now = economy._utcnow()
        fresh, new_anchor = economy.regenerate(charges, anchor, now)

        if fresh < cost:
            if (fresh, new_anchor) != (charges, anchor):
                c.execute("UPDATE users SET pull_charges = ?, pull_charges_at = ? WHERE id = ?",
                          (fresh, new_anchor, user_id))
                conn.commit()
            return False, {
                "charges": fresh, "cap": economy.PULL_CAP,
                "next_in": economy.seconds_to_next(fresh, new_anchor, now),
                "full_in": economy.seconds_to_full(fresh, new_anchor, now),
            }

        # Dropping below the cap starts the clock: a player spending from a full
        # stack should wait a whole interval for the next charge, not inherit an
        # anchor that was being reset while they sat at the cap.
        remaining = fresh - cost
        if fresh >= economy.PULL_CAP:
            new_anchor = now

        c.execute("UPDATE users SET pull_charges = ?, pull_charges_at = ? WHERE id = ?",
                  (remaining, new_anchor, user_id))
        conn.commit()
        return True, {
            "charges": remaining, "cap": economy.PULL_CAP,
            "next_in": economy.seconds_to_next(remaining, new_anchor, now),
            "full_in": economy.seconds_to_full(remaining, new_anchor, now),
        }
    finally:
        conn.close()


def add_pull_charges(user_id, count=1):
    """Give charges back (a refund) or hand them out (an admin grant).

    Capped like any other gain, and it does not touch the anchor: a refund
    should not also reset how far along the next charge was.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    try:
        c = conn.cursor()
        state = _read_pull_row(c, user_id)
        if state is None:
            return None
        charges, anchor = state
        fresh, new_anchor = economy.regenerate(charges, anchor)
        total = max(0, min(fresh + count, economy.PULL_CAP))
        c.execute("UPDATE users SET pull_charges = ?, pull_charges_at = ? WHERE id = ?",
                  (total, new_anchor, user_id))
        conn.commit()
        return total
    finally:
        conn.close()


# ===== MYTHIC COPY MANAGEMENT FUNCTIONS =====

def get_mythic_copy_count(song_id):
    """Get the current number of mythic copies that have been collected for a song"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) 
        FROM collections 
        WHERE song_id = ? AND variant = 'mythic'
    """, (song_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def get_next_mythic_copy_number(song_id):
    """Get what the next copy number would be for a mythic song"""
    return get_mythic_copy_count(song_id) + 1

def can_collect_mythic(song_id, max_copies=None):
    """Check if a mythic song can still be collected (hasn't reached max copies)"""
    max_copies = odds.MYTHIC_MAX_COPIES if max_copies is None else max_copies
    current_copies = get_mythic_copy_count(song_id)
    return current_copies < max_copies

def get_mythic_pull_state(song_id, user_id, max_copies=None):
    """Everything the pull path needs about one mythic, in a single round trip.

    Resolving a mythic used to ask three questions separately -- can it still be
    collected, does this user already hold one, and what rarity is it -- and two
    of those ran the identical COUNT twice. Over a pooled connection to a remote
    database each was its own ~50ms round trip, which is most of the wait before
    the reveal even starts rendering.

    The rarity arrives as a scalar subquery so a song with no mythic copies yet
    still reports one: an aggregate over an empty collections match would
    otherwise give a row of zeroes and no rarity.

    Returns the same keys as get_mythic_copy_info(), plus 'rarity' and
    'user_owns'.
    """
    max_copies = odds.MYTHIC_MAX_COPIES if max_copies is None else max_copies
    user_id = _uid(user_id)

    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT (SELECT rarity FROM songs WHERE id = ?),
               COUNT(*),
               COUNT(CASE WHEN user_id = ? THEN 1 END)
        FROM collections
        WHERE song_id = ? AND variant = 'mythic'
    """, (song_id, user_id, song_id))
    row = c.fetchone()
    conn.close()

    rarity, current_copies, mine = (row or (None, 0, 0))
    current_copies = current_copies or 0
    return {
        'rarity': rarity,
        'max_copies': max_copies,
        'current_copies': current_copies,
        'remaining_copies': max_copies - current_copies,
        'can_collect': current_copies < max_copies,
        'next_copy_number': current_copies + 1,
        'user_owns': bool(mine),
    }


def get_mythic_copy_info(song_id, max_copies=None):
    """Get information about mythic copies for a song"""
    max_copies = odds.MYTHIC_MAX_COPIES if max_copies is None else max_copies
    current_copies = get_mythic_copy_count(song_id)
    return {
        'max_copies': max_copies,
        'current_copies': current_copies,
        'remaining_copies': max_copies - current_copies,
        'can_collect': current_copies < max_copies,
        'next_copy_number': current_copies + 1
    }

def get_user_mythic_copy_number(user_id, song_id):
    """Which mythic copy of `song_id` this user holds, or None if they hold none.

    Two things this has to get right:
      * a non-owner must return None. The old single-query form compared
        against a NULL timestamp, matched no rows, and reported copy #1 for
        everyone who owned nothing.
      * collected_at only has one-second resolution, so two claims in the same
        second tie. collections.id breaks the tie, giving the same order
        get_mythic_owners lists them in.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT collected_at, id
        FROM collections
        WHERE user_id = ? AND song_id = ? AND variant = 'mythic'
        ORDER BY collected_at ASC, id ASC
        LIMIT 1
    """, (user_id, song_id))
    row = c.fetchone()
    if not row:
        conn.close()
        return None

    claimed_at, row_id = row
    c.execute("""
        SELECT COUNT(*) + 1
        FROM collections
        WHERE song_id = ? AND variant = 'mythic'
          AND (collected_at < ? OR (collected_at = ? AND id < ?))
    """, (song_id, claimed_at, claimed_at, row_id))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def get_mythic_owners(song_id):
    """Get list of users who own copies of a mythic song, ordered by collection time"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT user_id, collected_at
        FROM collections
        WHERE song_id = ? AND variant = 'mythic'
        ORDER BY collected_at ASC, id ASC
    """, (song_id,))
    results = c.fetchall()
    conn.close()
    return results  # List of (user_id, collected_at)

def get_collection_with_copy_numbers(user_id):
    """A user's collection, rarest tier first.

    Returns (song_name, artist, variant, album_name, album_image, copy_number,
    rarity, collection_id). The first six positions are unchanged, so callers
    that unpack entry[:6] keep working.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT 
            songs.name, 
            songs.artist, 
            collections.variant, 
            albums.name, 
            albums.image,
            CASE 
                WHEN collections.variant = 'mythic' THEN
                    (SELECT COUNT(*) + 1
                     FROM collections c2
                     WHERE c2.song_id = collections.song_id 
                     AND c2.variant = 'mythic' 
                     AND c2.collected_at < collections.collected_at)
                ELSE 1
            END as copy_number,
            songs.rarity,
            collections.id
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ?
        ORDER BY
            CASE songs.rarity
                WHEN 'ultimate' THEN 1
                WHEN 'legendary' THEN 2
                WHEN 'elite' THEN 3
                WHEN 'unique' THEN 4
                WHEN 'basic' THEN 5
                ELSE 6
            END ASC,
            songs.artist ASC,
            songs.name ASC,
            albums.name ASC
    """, (user_id,))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_name, artist_name, variant, album_name, album_image, copy_number)

def get_available_mythic_songs():
    """Get all songs that can still be collected as mythics (have less than 3 copies)"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT s.id, s.name, s.artist
        FROM songs s
        WHERE s.id NOT IN (
            SELECT song_id 
            FROM collections 
            WHERE variant = 'mythic'
            GROUP BY song_id
            HAVING COUNT(*) >= 3
        )
    """)
    results = c.fetchall()
    conn.close()
    return results  # List of (song_id, song_name, artist_name)

def get_available_mythic_songs_for_user(user_id):
    """Get all songs that can still be collected as mythics for a specific user 
    (have less than 3 copies AND user doesn't already own one)"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT s.id, s.name, s.artist
        FROM songs s
        WHERE s.id NOT IN (
            SELECT song_id 
            FROM collections 
            WHERE variant = 'mythic'
            GROUP BY song_id
            HAVING COUNT(*) >= 3
        )
        AND s.id NOT IN (
            SELECT song_id
            FROM collections
            WHERE user_id = ? AND variant = 'mythic'
        )
    """, (user_id,))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_id, song_name, artist_name)

def user_owns_mythic(user_id, song_id):
    """Check if a user already owns a mythic copy of a specific song"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) 
        FROM collections 
        WHERE user_id = ? AND song_id = ? AND variant = 'mythic'
    """, (user_id, song_id))
    result = c.fetchone()
    conn.close()
    return result[0] > 0 if result else False

# ===== VINYL TRACKING FUNCTIONS =====

def add_vinyl_count(user_id, count=1):
    """Add vinyl pulls to a user's available count"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()

    # Add to vinyl count (column is created in init_db)
    c.execute("""
        UPDATE users 
        SET vinyl_count = COALESCE(vinyl_count, 0) + ?
        WHERE id = ?
    """, (count, user_id))
    conn.commit()
    conn.close()

def get_vinyl_count(user_id):
    """Get a user's available vinyl pull count"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COALESCE(vinyl_count, 0) FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def use_vinyl_pull(user_id):
    """Use one vinyl pull from user's count. Returns True if successful, False if no pulls left"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    
    # Check current count
    current_count = get_vinyl_count(user_id)
    if current_count <= 0:
        conn.close()
        return False
    
    # Decrease count
    c.execute("""
        UPDATE users 
        SET vinyl_count = vinyl_count - 1
        WHERE id = ? AND vinyl_count > 0
    """, (user_id,))
    
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    return success

def add_sig_vinyl_count(user_id, count=1):
    """Add signature vinyl pulls to a user's available count"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()

    # Add to sig vinyl count (column is created in init_db)
    c.execute("""
        UPDATE users
        SET sig_vinyl_count = COALESCE(sig_vinyl_count, 0) + ?
        WHERE id = ?
    """, (count, user_id))
    conn.commit()
    conn.close()


def get_sig_vinyl_count(user_id):
    """Get a user's available signature vinyl pull count"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COALESCE(sig_vinyl_count, 0) FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def use_sig_vinyl_pull(user_id):
    """Use one signature vinyl pull from user's count. Returns True if successful, False if no pulls left"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    
    # Check current count
    current_count = get_sig_vinyl_count(user_id)
    if current_count <= 0:
        conn.close()
        return False
    
    # Decrease count
    c.execute("""
        UPDATE users 
        SET sig_vinyl_count = sig_vinyl_count - 1
        WHERE id = ? AND sig_vinyl_count > 0
    """, (user_id,))
    
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    return success

def list_latest(limit=10):
    """List the latest songs added to the collection across all users (default 10)"""
    conn = get_connection()
    c = conn.cursor()
    # Extra columns are appended, never inserted, so existing positional
    # unpacking of the first six keeps working (see cogs/mod_cog.py).
    # users is LEFT JOINed: a collector who has since been unregistered still
    # has collection rows, and those pulls must not vanish from this list.
    c.execute("""
        SELECT collections.id, songs.name, songs.artist, collections.variant, albums.name,
               collections.collected_at, albums.image, collections.user_id, users.username
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        LEFT JOIN users ON users.id = collections.user_id
        ORDER BY collections.collected_at DESC
        LIMIT ?
    """, (limit,))
    results = c.fetchall()
    conn.close()
    # (collection_id, song_name, artist, variant, album_name, collected_at,
    #  album_image, user_id, username)
    return results

def remove_latest(n=1):
    """Remove the latest n songs added to the collection across all users"""
    conn = get_connection()
    c = conn.cursor()
    # Get the latest n collection ids
    c.execute("""
        SELECT id FROM collections
        ORDER BY collected_at DESC
        LIMIT ?
    """, (n,))
    ids = [row[0] for row in c.fetchall()]
    if ids:
        placeholders = ','.join(['?' for _ in ids])
        c.execute(f"DELETE FROM collections WHERE id IN ({placeholders})", ids)
        conn.commit()
    conn.close()
    return len(ids)  # Number of items

def get_songs_with_ids_by_artist(artist_name):
    """Get all songs by an artist with id, rarity, album name, and track count (for admin/catalog views)"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT songs.id, songs.name, songs.rarity, albums.name as album_name,
               COUNT(song_album.song_id) OVER (PARTITION BY albums.id) as track_count,
               albums.image, albums.id as album_id
        FROM songs
        JOIN song_album ON songs.id = song_album.song_id
        JOIN albums ON song_album.album_id = albums.id
        WHERE songs.artist = ? COLLATE NOCASE
        ORDER BY albums.name, songs.name
    """, (artist_name,))
    results = c.fetchall()
    conn.close()
    # album_id is what the catalog UI keys its album panes on: two albums by the
    # same artist can share a name (different pressings), but never an id.
    return results  # List of (song_id, song_name, rarity, album_name, track_count, album_image, album_id)

def get_all_users():
    """List all registered users with their xp/vinyl/sig_vinyl counts"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, username, xp, vinyl_count, sig_vinyl_count FROM users ORDER BY xp DESC")
    results = c.fetchall()
    conn.close()
    return results  # List of (id, username, xp, vinyl_count, sig_vinyl_count)

def get_top_users_by_xp(limit=10):
    """The highest-XP registered users, best first.

    Ordered and limited in SQL rather than by sorting every user in Python, so
    a large player base costs the leaderboard nothing.

    Ties break on id so two users on identical XP keep a stable order between
    calls instead of swapping places at random.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT id, username, COALESCE(xp, 0)
        FROM users
        ORDER BY COALESCE(xp, 0) DESC, id ASC
        LIMIT ?
    """, (int(limit),))
    results = c.fetchall()
    conn.close()
    return results  # List of (id, username, xp)


def get_user_xp_rank(user_id):
    """1-based leaderboard position, or None if the user is not registered.

    Counts the users strictly ahead rather than materialising the whole table,
    matching the tie-break of get_top_users_by_xp so a user's own rank agrees
    with where they appear in the list.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COALESCE(xp, 0) FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        conn.close()
        return None
    xp = row[0]
    c.execute("""
        SELECT COUNT(*) FROM users
        WHERE COALESCE(xp, 0) > ?
           OR (COALESCE(xp, 0) = ? AND id < ?)
    """, (xp, xp, user_id))
    ahead = c.fetchone()[0]
    conn.close()
    return ahead + 1


def unregister_user(user_id):
    """Remove a user from the users table (their collection history is left intact)"""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    return success

def search_songs(query, limit=20):
    """Search songs by name or artist (case-insensitive substring match), with album info if linked"""
    conn = get_connection()
    c = conn.cursor()
    like = f"%{query}%"
    c.execute("""
        SELECT songs.id, songs.name, songs.artist, songs.rarity,
               albums.id, albums.name, albums.image
        FROM songs
        LEFT JOIN song_album ON song_album.song_id = songs.id
        LEFT JOIN albums ON song_album.album_id = albums.id
        WHERE songs.name LIKE ? COLLATE NOCASE OR songs.artist LIKE ? COLLATE NOCASE
        LIMIT ?
    """, (like, like, limit))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_id, song_name, artist, rarity, album_id, album_name, album_image)

def get_mythic_hunt():
    """Get the currently set mythic hunt, if any"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT song_id, song_name, album_id, album_name, album_url, artist FROM mythic_state WHERE id = 1")
    result = c.fetchone()
    conn.close()
    if not result or not result[0]:
        return None
    return {
        'song_id': result[0],
        'song_name': result[1],
        'album_id': result[2],
        'album_name': result[3],
        'album_url': result[4],
        'artist': result[5],
    }

def set_mythic_hunt(song_id, song_name, album_id, album_name, album_url, artist):
    """Persist the next mythic hunt target"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO mythic_state (id, song_id, song_name, album_id, album_name, album_url, artist)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            song_id=excluded.song_id, song_name=excluded.song_name,
            album_id=excluded.album_id, album_name=excluded.album_name,
            album_url=excluded.album_url, artist=excluded.artist
    """, (song_id, song_name, album_id, album_name, album_url, artist))
    conn.commit()
    conn.close()

def clear_mythic_hunt():
    """Clear the current mythic hunt target"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO mythic_state (id, song_id, song_name, album_id, album_name, album_url, artist)
        VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL)
        ON CONFLICT(id) DO UPDATE SET
            song_id=NULL, song_name=NULL, album_id=NULL, album_name=NULL, album_url=NULL, artist=NULL
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Card / profile queries
#
# Everything below addresses a card by collections.id -- the same identifier
# /trade already autocompletes on -- so one addressing scheme covers view,
# trade and pinning.
# ---------------------------------------------------------------------------

# Browsing order for a collection: rarest tier first. Written as a CASE so the
# same expression works on SQLite and Postgres.
_RARITY_RANK_SQL = """
    CASE songs.rarity
        WHEN 'ultimate' THEN 1
        WHEN 'legendary' THEN 2
        WHEN 'elite' THEN 3
        WHEN 'unique' THEN 4
        WHEN 'basic' THEN 5
        ELSE 6
    END
"""

_CARD_COLUMNS = """
    collections.id, collections.user_id, songs.id, songs.name, songs.artist,
    songs.rarity, collections.variant, albums.id, albums.name, albums.image,
    collections.collected_at
"""


def _card_dict(row):
    """Map a _CARD_COLUMNS row onto names, so callers never index by position."""
    if not row:
        return None
    return {
        "collection_id": row[0],
        "user_id": row[1],
        "song_id": row[2],
        "song_name": row[3],
        "artist": row[4],
        "rarity": row[5],
        "variant": row[6],
        "album_id": row[7],
        "album_name": row[8],
        "album_image": row[9],
        "collected_at": row[10],
    }


def get_card(collection_id, user_id=None):
    """One card by its collection id.

    Pass user_id to require ownership (the pin action does); leave it off to
    look at someone else's card, which /view supports.
    """
    conn = get_connection()
    c = conn.cursor()
    sql = f"""
        SELECT {_CARD_COLUMNS}
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.id = ?
    """
    params = [collection_id]
    if user_id is not None:
        sql += " AND collections.user_id = ?"
        params.append(_uid(user_id))
    c.execute(sql, tuple(params))
    row = c.fetchone()
    conn.close()
    return _card_dict(row)


def get_latest_card(user_id):
    """The user's most recent pull -- what a bare /view shows."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(f"""
        SELECT {_CARD_COLUMNS}
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ?
        ORDER BY collections.collected_at DESC, collections.id DESC
        LIMIT 1
    """, (_uid(user_id),))
    row = c.fetchone()
    conn.close()
    return _card_dict(row)


def search_user_cards(user_id, query="", variant=None, limit=25, artist=None):
    """Cards in one user's collection matching free text, rarest first.

    Backs both the /view autocomplete and the `.view <text>` prefix form, so the
    two always agree on what a given string resolves to.
    """
    conn = get_connection()
    c = conn.cursor()
    sql = f"""
        SELECT {_CARD_COLUMNS}
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ?
    """
    params = [_uid(user_id)]
    if variant:
        sql += " AND collections.variant = ?"
        params.append(variant)
    if artist:
        sql += " AND songs.artist = ? COLLATE NOCASE"
        params.append(artist)
    if query:
        like = f"%{query}%"
        sql += """ AND (songs.name LIKE ? COLLATE NOCASE
                     OR songs.artist LIKE ? COLLATE NOCASE
                     OR albums.name LIKE ? COLLATE NOCASE)"""
        params += [like, like, like]
    sql += f" ORDER BY {_RARITY_RANK_SQL} ASC, songs.artist ASC, songs.name ASC LIMIT ?"
    params.append(limit)
    c.execute(sql, tuple(params))
    rows = c.fetchall()
    conn.close()
    return [_card_dict(r) for r in rows]


def count_card_owners(song_id, variant=None):
    """How many copies of this song are held across all players."""
    conn = get_connection()
    c = conn.cursor()
    if variant:
        c.execute("SELECT COUNT(*) FROM collections WHERE song_id = ? AND variant = ?",
                  (song_id, variant))
    else:
        c.execute("SELECT COUNT(*) FROM collections WHERE song_id = ?", (song_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


def get_card_copy_number(collection_id):
    """Which mythic copy this row is, by claim order. None for other variants."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT (SELECT COUNT(*) + 1
                FROM collections c2
                WHERE c2.song_id = c1.song_id
                  AND c2.variant = 'mythic'
                  AND c2.collected_at < c1.collected_at)
        FROM collections c1
        WHERE c1.id = ? AND c1.variant = 'mythic'
    """, (collection_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


# ---- profile --------------------------------------------------------------

def set_pinned_card(user_id, collection_id):
    """Pin one of the user's own cards to their profile. Ownership is enforced
    in SQL so a crafted id cannot pin someone else's card."""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE users SET pinned_collection_id = ?
        WHERE id = ? AND EXISTS (
            SELECT 1 FROM collections WHERE id = ? AND user_id = ?
        )
    """, (collection_id, user_id, collection_id, user_id))
    ok = c.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def clear_pinned_card(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET pinned_collection_id = NULL WHERE id = ?", (_uid(user_id),))
    conn.commit()
    conn.close()


def set_favorite_artist(user_id, artist):
    """Set (or clear, with None) the artist a user chooses to show off."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET favorite_artist = ? WHERE id = ?", (artist, _uid(user_id)))
    ok = c.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def get_user_profile(user_id):
    """Everything /profile needs, in four queries rather than one per stat."""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT username, xp, vinyl_count, sig_vinyl_count,
               pinned_collection_id, favorite_artist
        FROM users WHERE id = ?
    """, (user_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    username, xp, vinyl, sig_vinyl, pinned_id, favorite = row

    # Totals plus the rarity spread, in one pass over the user's collection.
    c.execute("""
        SELECT COUNT(*), COUNT(DISTINCT collections.song_id),
               SUM(CASE WHEN collections.variant = 'mythic' THEN 1 ELSE 0 END)
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        WHERE collections.user_id = ?
    """, (user_id,))
    cards, unique_songs, mythics = c.fetchone() or (0, 0, 0)

    c.execute("""
        SELECT songs.rarity, COUNT(*)
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        WHERE collections.user_id = ?
        GROUP BY songs.rarity
    """, (user_id,))
    rarity_counts = {r: n for r, n in c.fetchall()}

    # Most-owned artist: earned, as opposed to the chosen favorite above.
    c.execute("""
        SELECT songs.artist, COUNT(*) AS n
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        WHERE collections.user_id = ?
        GROUP BY songs.artist
        ORDER BY n DESC, songs.artist ASC
        LIMIT 1
    """, (user_id,))
    top = c.fetchone()

    c.execute("""
        SELECT MIN(collected_at) FROM collections WHERE user_id = ?
    """, (user_id,))
    since = (c.fetchone() or (None,))[0]

    conn.close()
    return {
        "user_id": user_id,
        "username": username,
        "xp": xp or 0,
        "vinyl_count": vinyl or 0,
        "sig_vinyl_count": sig_vinyl or 0,
        "pinned_collection_id": pinned_id,
        "favorite_artist": favorite,
        "cards": cards or 0,
        "unique_songs": unique_songs or 0,
        "mythics": mythics or 0,
        "rarity_counts": rarity_counts,
        "top_artist": top[0] if top else None,
        "top_artist_count": top[1] if top else 0,
        "collecting_since": since,
    }


def get_artist_completion(user_id, artist):
    """How much of one artist's catalog a user holds: (owned, total)."""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM songs WHERE artist = ? COLLATE NOCASE", (artist,))
    total = (c.fetchone() or (0,))[0] or 0
    c.execute("""
        SELECT COUNT(DISTINCT collections.song_id)
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        WHERE collections.user_id = ? AND songs.artist = ? COLLATE NOCASE
    """, (user_id, artist))
    owned = (c.fetchone() or (0,))[0] or 0
    conn.close()
    return owned, total


def get_user_collection_artists(user_id):
    """Artists the user actually owns something by, for autocomplete."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT songs.artist
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        WHERE collections.user_id = ?
        ORDER BY songs.artist
    """, (_uid(user_id),))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_random_song_candidates(pairs):
    """Candidate songs for many (artist, rarity) pairs in a single query.

    The pull flow used to run one query per pick, then another for the album
    and another for the rarity -- nine round-trips for three songs. This
    resolves the whole draw in one, returning {(artist_lower, rarity_lower):
    [rows]} so the caller can pick per pair in memory.

    Album columns are joined here too, so no follow-up lookup is needed. The
    INNER JOIN matches get_songs_by_artist_and_rarity: a song with no album is
    not a valid pull either way, and a song on several albums appears once per
    album, which keeps the existing album-count weighting.
    """
    if not pairs:
        return {}

    # De-duplicate: the same (artist, rarity) can be drawn twice, and one
    # clause covers both draws.
    unique = list(dict.fromkeys((a, r) for a, r in pairs))
    clause = " OR ".join(
        ["(songs.artist = ? COLLATE NOCASE AND songs.rarity = ? COLLATE NOCASE)"] * len(unique)
    )
    params = [value for pair in unique for value in pair]

    conn = get_connection()
    c = conn.cursor()
    c.execute(f"""
        SELECT songs.artist, songs.rarity, songs.id, songs.name,
               albums.id, albums.name, albums.image
        FROM songs
        JOIN song_album ON songs.id = song_album.song_id
        JOIN albums ON song_album.album_id = albums.id
        WHERE {clause}
    """, tuple(params))
    rows = c.fetchall()
    conn.close()

    buckets = {}
    for artist, rarity, song_id, song_name, album_id, album_name, album_image in rows:
        key = ((artist or "").lower(), (rarity or "").lower())
        buckets.setdefault(key, []).append({
            "song_id": song_id,
            "song_name": song_name,
            "artist": artist,
            "rarity": rarity,
            "album_id": album_id,
            "album_name": album_name,
            "album_image": album_image,
        })
    return buckets
