import os
import re
import sqlite3
import threading

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


def _translate_sql(sql):
    """Rewrite a SQLite statement for Postgres.

    Returns None for statements that have no Postgres equivalent (PRAGMA), which
    the cursor wrapper then skips. String literals are left untouched so a '?' or
    the word COLLATE inside quoted text is never rewritten.
    """
    if sql.lstrip().upper().startswith("PRAGMA"):
        return None

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
        translated = _translate_sql(sql)
        if translated is None:  # PRAGMA and friends: no-op on Postgres
            return self
        self._raw.execute(translated, tuple(params))
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


def get_connection():
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
        rarity TEXT,
        UNIQUE(name, artist)
    )""",

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
        sig_vinyl_count INTEGER DEFAULT 0
    )""",

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


def _init_postgres():
    """Create the schema on Postgres. Mirrors the SQLite schema below, with
    AUTOINCREMENT -> IDENTITY and the name columns typed CITEXT."""
    conn = get_connection()
    try:
        c = conn.cursor()
        for statement in _POSTGRES_SCHEMA:
            c.execute(statement)
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
        sig_vinyl_count INTEGER DEFAULT 0
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

def add_song(song_id, name, artist, rarity="common"):
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
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO collections (user_id, song_id, album_id, variant)
        VALUES (?, ?, ?, ?)
    """, (user_id, song_id, album_id, variant))
    conn.commit()
    conn.close()

def get_collection_by_user(user_id):
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
            END as copy_number
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ? AND collections.variant = ?
        ORDER BY
            songs.artist ASC,
            songs.name ASC,
            albums.name ASC
    """, (user_id, rarity))
    results = c.fetchall()
    conn.close()
    return results

def get_collection_by_artist_with_copy_numbers(user_id, artist_name):
    """Get user's collection filtered by artist, including copy numbers for mythic items"""
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
            END as copy_number
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ? AND LOWER(songs.artist) LIKE LOWER(?)
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
            songs.name ASC,
            albums.name ASC
    """, (user_id, f"%{artist_name}%"))
    results = c.fetchall()
    conn.close()
    return results

def get_collection_by_rarity_and_artist_with_copy_numbers(user_id, rarity, artist_name):
    """Get user's collection filtered by rarity and artist, including copy numbers for mythic items"""
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
            END as copy_number
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ? AND collections.variant = ? AND LOWER(songs.artist) LIKE LOWER(?)
        ORDER BY
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
    """Check if user has a specific song with the given rarity"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT collections.id, songs.id as song_id, albums.id as album_id, songs.name, songs.artist, collections.variant, albums.name as album_name
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
    (collection_id, song_id, album_id, song_name, artist_name, variant, album_name)."""
    conn = get_connection()
    c = conn.cursor()
    if artist:
        c.execute("""
            SELECT collections.id, songs.id, albums.id, songs.name, songs.artist, collections.variant, albums.name
            FROM collections
            JOIN songs ON collections.song_id = songs.id
            JOIN albums ON collections.album_id = albums.id
            WHERE collections.user_id = ? AND collections.variant = ? AND songs.artist = ? COLLATE NOCASE
            ORDER BY songs.name ASC
        """, (str(user_id), rarity, artist))
    else:
        c.execute("""
            SELECT collections.id, songs.id, albums.id, songs.name, songs.artist, collections.variant, albums.name
            FROM collections
            JOIN songs ON collections.song_id = songs.id
            JOIN albums ON collections.album_id = albums.id
            WHERE collections.user_id = ? AND collections.variant = ?
            ORDER BY songs.artist ASC, songs.name ASC
        """, (str(user_id), rarity))
    results = c.fetchall()
    conn.close()
    return results

def get_collection_item_by_id(collection_id, user_id):
    """Get a single collection row by its id, scoped to a user (ownership check).
    Same column order as get_user_song_by_details."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT collections.id, songs.id, albums.id, songs.name, songs.artist, collections.variant, albums.name
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.id = ? AND collections.user_id = ?
    """, (collection_id, str(user_id)))
    result = c.fetchone()
    conn.close()
    return result

def remove_from_collection(user_id, song_id, album_id, variant):
    """Remove a specific item from user's collection"""
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
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT xp FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def update_user_xp(user_id, xp):
    """Update the XP of a user"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET xp = ? WHERE id = ?", (xp, user_id))
    rows_affected = c.rowcount
    conn.commit()
    conn.close()
    return rows_affected > 0

def add_user_xp(user_id, xp_to_add):
    """Add XP to a user's current XP"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET xp = xp + ? WHERE id = ?", (xp_to_add, user_id))
    rows_affected = c.rowcount
    conn.commit()
    conn.close()
    return rows_affected > 0

def get_user_stats(user_id):
    """Get user's xp"""
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
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def get_last_daily_claim(user_id):
    """Get the last daily claim timestamp for a user"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT last_daily_claim FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def update_daily_claim(user_id):
    """Update the last daily claim timestamp for a user to now"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET last_daily_claim = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
    rows_affected = c.rowcount
    conn.commit()
    conn.close()
    return rows_affected > 0

def can_claim_daily(user_id):
    """Check if user can claim daily reward (24 hours since last claim)"""
    import datetime
    
    last_claim = get_last_daily_claim(user_id)
    if not last_claim:
        return True, 0  # Never claimed before
    
    # Parse the timestamp
    try:
        # Parse the SQLite timestamp as naive UTC datetime
        last_claim_dt = datetime.datetime.fromisoformat(last_claim.replace('Z', ''))
        
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

def can_collect_mythic(song_id, max_copies=3):
    """Check if a mythic song can still be collected (hasn't reached max copies)"""
    current_copies = get_mythic_copy_count(song_id)
    return current_copies < max_copies

def get_mythic_copy_info(song_id, max_copies=3):
    """Get information about mythic copies for a song"""
    current_copies = get_mythic_copy_count(song_id)
    return {
        'max_copies': max_copies,
        'current_copies': current_copies,
        'remaining_copies': max_copies - current_copies,
        'can_collect': current_copies < max_copies,
        'next_copy_number': current_copies + 1
    }

def get_user_mythic_copy_number(user_id, song_id):
    """Get which copy number a user has for a mythic song (based on claim order)"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) + 1
        FROM collections c1
        WHERE c1.song_id = ? AND c1.variant = 'mythic' 
        AND c1.collected_at < (
            SELECT c2.collected_at 
            FROM collections c2 
            WHERE c2.user_id = ? AND c2.song_id = ? AND c2.variant = 'mythic'
            LIMIT 1
        )
    """, (song_id, user_id, song_id))
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
        ORDER BY collected_at ASC
    """, (song_id,))
    results = c.fetchall()
    conn.close()
    return results  # List of (user_id, collected_at)

def get_collection_with_copy_numbers(user_id):
    """Get user's collection including copy numbers for mythic items (based on collection order)"""
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
            END as copy_number
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
    conn = get_connection()
    c = conn.cursor()

    # Add to vinyl count (column is created in init_db)
    c.execute("""
        UPDATE users 
        SET vinyl_count = COALESCE(vinyl_count, 0) + ?
        WHERE id = ?
    """, (count, str(user_id)))
    conn.commit()
    conn.close()

def get_vinyl_count(user_id):
    """Get a user's available vinyl pull count"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COALESCE(vinyl_count, 0) FROM users WHERE id = ?", (str(user_id),))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def use_vinyl_pull(user_id):
    """Use one vinyl pull from user's count. Returns True if successful, False if no pulls left"""
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
    """, (str(user_id),))
    
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    return success

def add_sig_vinyl_count(user_id, count=1):
    """Add signature vinyl pulls to a user's available count"""
    conn = get_connection()
    c = conn.cursor()

    # Add to sig vinyl count (column is created in init_db)
    c.execute("""
        UPDATE users
        SET sig_vinyl_count = COALESCE(sig_vinyl_count, 0) + ?
        WHERE id = ?
    """, (count, str(user_id)))
    conn.commit()
    conn.close()


def get_sig_vinyl_count(user_id):
    """Get a user's available signature vinyl pull count"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COALESCE(sig_vinyl_count, 0) FROM users WHERE id = ?", (str(user_id),))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def use_sig_vinyl_pull(user_id):
    """Use one signature vinyl pull from user's count. Returns True if successful, False if no pulls left"""
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
    """, (str(user_id),))
    
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    return success

def list_latest(limit=10):
    """List the latest songs added to the collection across all users (default 10)"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT collections.id, songs.name, songs.artist, collections.variant, albums.name, collections.collected_at
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        ORDER BY collections.collected_at DESC
        LIMIT ?
    """, (limit,))
    results = c.fetchall()
    conn.close()
    return results  # List of (collection_id, song_name, artist, variant, album_name, collected_at)

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
               COUNT(song_album.song_id) OVER (PARTITION BY albums.id) as track_count
        FROM songs
        JOIN song_album ON songs.id = song_album.song_id
        JOIN albums ON song_album.album_id = albums.id
        WHERE songs.artist = ? COLLATE NOCASE
        ORDER BY albums.name, songs.name
    """, (artist_name,))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_id, song_name, rarity, album_name, track_count)

def get_all_users():
    """List all registered users with their xp/vinyl/sig_vinyl counts"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, username, xp, vinyl_count, sig_vinyl_count FROM users ORDER BY xp DESC")
    results = c.fetchall()
    conn.close()
    return results  # List of (id, username, xp, vinyl_count, sig_vinyl_count)

def unregister_user(user_id):
    """Remove a user from the users table (their collection history is left intact)"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id = ?", (str(user_id),))
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

