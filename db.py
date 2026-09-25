import contextlib
import datetime as _datetime
import os
import re
import sqlite3
import threading
import time
import uuid

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
                    #
                    # autocommit: without it psycopg opens a transaction before the
                    # first statement and close() has to roll it back, so every
                    # function paid BEGIN + query + ROLLBACK -- three round trips
                    # to the database where one does the job. Measured against
                    # Supabase that was ~164ms per call against ~52ms. The few
                    # functions that need several writes to land together open
                    # their own transaction with _begin().
                    kwargs={"prepare_threshold": None, "autocommit": True},
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
# shared_connection() pins one connection to the current thread for the
# duration of a block; get_connection() then hands out a non-closing view of
# it, so none of the existing functions need to change.
#
# It was written to cut a per-call cost that turned out to be the transaction
# wrapper rather than the connection: checking a connection out of the pool is
# effectively free, but every call used to pay BEGIN and ROLLBACK around its
# query, and sharing a connection shared the transaction. With the pool in
# autocommit that cost is gone for every call, so on Postgres this is now
# neutral. It stays because it is harmless, existing callers use it, and on
# SQLite it still saves reopening the database file.
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


def _begin(conn):
    """Open an explicit transaction for a function whose writes must all land
    or none of them.

    Postgres connections run in autocommit, so each statement commits on its
    own. A function that issues several dependent writes calls this first; its
    existing commit() and rollback() then end the transaction exactly as they
    did before. A no-op when a transaction is already open -- a nested call
    inside shared_connection(), or SQLite having begun one implicitly.
    """
    inner = conn._inner if isinstance(conn, _SharedConnection) else conn
    if isinstance(inner, _PooledConnection):
        from psycopg.pq import TransactionStatus
        if inner._raw.info.transaction_status == TransactionStatus.IDLE:
            inner._raw.execute("BEGIN")
    elif not inner.in_transaction:          # sqlite3.Connection
        inner.execute("BEGIN")


def _open_connection():
    if IS_POSTGRES:
        pool = _get_pg_pool()
        return _PooledConnection(pool, pool.getconn())

    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite needs this per-connection to enforce FKs
    return conn

# ---- Postgres schema guards ---------------------------------------------------
#
# The schema below runs on every start, and most of it is already in place. That
# used to cost more than time: measured against a scratch table, an
# "ALTER TABLE ... ADD COLUMN IF NOT EXISTS" whose column already exists still
# takes an ACCESS EXCLUSIVE lock -- blocking even plain reads -- and so does
# "DROP CONSTRAINT IF EXISTS" on an absent constraint, while
# "CREATE INDEX IF NOT EXISTS" on an existing index takes a SHARE lock, which
# blocks writes. Every boot was briefly locking users and songs against every
# query; pg_stat_statements showed the constraint drop waiting up to 1.2s.
#
# These wrap each such statement in a DO block that checks the catalog first,
# which takes no lock at all, so a boot with nothing to change touches nothing.
# The inner statement keeps its own IF [NOT] EXISTS so two instances starting at
# the same moment cannot trip over each other.

def _pg_add_column(table, column, definition):
    return (f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM information_schema.columns "
            f"WHERE table_schema = current_schema() AND table_name = '{table}' "
            f"AND column_name = '{column}') THEN "
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}; "
            f"END IF; END $$")


def _pg_drop_constraint(table, constraint):
    return (f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
            f"WHERE conname = '{constraint}' AND conrelid = '{table}'::regclass) THEN "
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}; "
            f"END IF; END $$")


def _pg_index(name, ddl):
    # Checked in pg_indexes against current_schema(), not with to_regclass():
    # that resolves through the whole search_path, so an index of the same name
    # in another schema would wrongly count as this one existing.
    return (f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes "
            f"WHERE schemaname = current_schema() AND indexname = '{name}') THEN "
            f"{ddl}; END IF; END $$")


# Gives every mythic copy that has no number yet the next ones for its song, in
# claim order (collected_at, then id). On the first boot that numbers every
# existing copy exactly as players already saw it; afterwards it only finds a
# copy inserted by a build that predates the column. Guarded like the schema
# statements, so a boot with nothing to number takes no lock.
BACKFILL_COPY_NUMBERS_PG = """DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM collections WHERE variant = 'mythic' AND copy_number IS NULL) THEN
        UPDATE collections c SET copy_number = n.num
        FROM (SELECT x.id,
                     COALESCE((SELECT MAX(y.copy_number) FROM collections y
                               WHERE y.song_id = x.song_id AND y.variant = 'mythic'), 0)
                     + ROW_NUMBER() OVER (PARTITION BY x.song_id
                                          ORDER BY x.collected_at, x.id) AS num
              FROM collections x
              WHERE x.variant = 'mythic' AND x.copy_number IS NULL) n
        WHERE c.id = n.id;
    END IF;
END $$"""


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
    _pg_drop_constraint("songs", "songs_name_artist_key"),
    _pg_index("songs_name_artist_unique",
              "CREATE UNIQUE INDEX IF NOT EXISTS songs_name_artist_unique "
              "ON songs ((name::text), (artist::text))"),

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
    _pg_add_column("users", "pinned_collection_id", "INTEGER DEFAULT NULL"),
    _pg_add_column("users", "favorite_artist", "TEXT DEFAULT NULL"),

    # Drop economy. pull_charges_at is the anchor the balance regenerates from;
    # NULL means "never spent", which utils/economy.py reads as a full stack, so
    # players who predate the feature are not punished for it.
    _pg_add_column("users", "pull_charges", "INTEGER DEFAULT 20"),
    _pg_add_column("users", "pull_charges_at", "TIMESTAMP DEFAULT NULL"),

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

    # A mythic's copy number is stored on its row, set once when it is claimed.
    # It used to be recomputed on every read by counting earlier claims, which
    # renumbered every later copy whenever one was deleted, and two readers
    # broke ties differently.
    _pg_add_column("collections", "copy_number", "INTEGER DEFAULT NULL"),
    BACKFILL_COPY_NUMBERS_PG,
    # One of each number per song. This is also what keeps two claims racing
    # for the same song honest: both pick the same number, the index refuses
    # the second, and it retries (see claim_mythic).
    _pg_index("collections_mythic_copy_unique",
              "CREATE UNIQUE INDEX IF NOT EXISTS collections_mythic_copy_unique "
              "ON collections (song_id, copy_number) WHERE variant = 'mythic'"),

    *(_pg_index(name, f"CREATE INDEX IF NOT EXISTS {name} ON {target}") for name, target in (
        ("idx_user", "collections(user_id)"),
        ("idx_song", "collections(song_id)"),
        ("idx_collections_user_variant", "collections(user_id, variant)"),
        ("idx_collections_song_variant", "collections(song_id, variant)"),
        # collections.album_id is a foreign key, and Postgres does not index
        # those itself: without this, removing an artist's albums scanned
        # every collection row to check nothing still pointed at them.
        ("idx_collections_album", "collections(album_id)"),
        ("idx_songs_artist_rarity", "songs(artist, rarity)"),
        ("idx_albums_artist", "albums(artist)"),
        ("idx_song_album_song", "song_album(song_id)"),
        ("idx_song_album_album", "song_album(album_id)"),
    )),
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
    AUTOINCREMENT -> IDENTITY and the name columns typed CITEXT.

    Sent as one multi-statement batch -- a single round trip rather than one
    per statement (24 of them, ~1.4s at boot). Postgres runs a batch like this
    as one implicit transaction, so it is still all-or-nothing, as it was when
    every statement shared one explicit transaction. It goes straight to the
    raw connection because it is Postgres SQL already and must not pass through
    the SQLite translation layer.
    """
    batch = ";\n".join((*_POSTGRES_SCHEMA, MIGRATE_RARITIES_SQL))
    with _get_pg_pool().connection() as raw:
        raw.execute(batch)


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

    # ---- mythic copy numbers, stored on the row (see _POSTGRES_SCHEMA) ----
    c.execute("PRAGMA table_info(collections)")
    if "copy_number" not in {row[1] for row in c.fetchall()}:
        c.execute("ALTER TABLE collections ADD COLUMN copy_number INTEGER DEFAULT NULL")
    _backfill_copy_numbers_sqlite(c)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS collections_mythic_copy_unique "
              "ON collections(song_id, copy_number) WHERE variant = 'mythic'")

    # ---- indexes for the common hot-path filters ----
    c.execute("CREATE INDEX IF NOT EXISTS idx_collections_user_variant ON collections(user_id, variant)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_collections_song_variant ON collections(song_id, variant)")
    # Foreign key, which neither backend indexes on its own. See _POSTGRES_SCHEMA.
    c.execute("CREATE INDEX IF NOT EXISTS idx_collections_album ON collections(album_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_songs_artist_rarity ON songs(artist, rarity)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_song_album_song ON song_album(song_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_song_album_album ON song_album(album_id)")

    c.execute(MIGRATE_RARITIES_SQL)

    conn.commit()
    conn.close()


def _backfill_copy_numbers_sqlite(c):
    """BACKFILL_COPY_NUMBERS_PG for SQLite, numbered in Python: a SQLite UPDATE
    whose subqueries read the table being changed sees its own earlier rows."""
    c.execute("""SELECT id, song_id FROM collections
                 WHERE variant = 'mythic' AND copy_number IS NULL
                 ORDER BY song_id, collected_at, id""")
    pending = c.fetchall()
    if not pending:
        return
    c.execute("""SELECT song_id, MAX(copy_number) FROM collections
                 WHERE variant = 'mythic' GROUP BY song_id""")
    last = {song_id: top or 0 for song_id, top in c.fetchall()}
    for collection_id, song_id in pending:
        last[song_id] = last.get(song_id, 0) + 1
        c.execute("UPDATE collections SET copy_number = ? WHERE id = ?",
                  (last[song_id], collection_id))


def _is_unique_violation(exc):
    """True when `exc` is a unique index refusing a row, on either backend."""
    if isinstance(exc, sqlite3.IntegrityError):
        return "UNIQUE" in str(exc).upper()
    return type(exc).__name__ == "UniqueViolation"


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


# ---------------------------------------------------------------------------
# Catalogue cache
#
# The artist list, the /artists overview and per-artist album counts are read
# constantly -- album autocomplete fires on every keystroke -- yet only change
# when an admin imports or removes an artist. Serving them from memory turns a
# database round trip into a dict lookup.
#
# Coherent within the process: every write to albums or song_album goes through
# this module and calls _invalidate_catalog(), and the web panel runs as a
# thread inside the bot's process, so it shares this cache too. The TTL only
# matters for writes that bypass the module entirely, such as a one-off script.
#
# The generation counter closes the one real race: a read that began before an
# invalidation must not store its now-stale result after it.
# ---------------------------------------------------------------------------

CATALOG_CACHE_TTL = 300  # seconds

_catalog_cache = {}
_catalog_generation = 0
_catalog_lock = threading.Lock()


def _invalidate_catalog():
    """Drop every cached catalogue read. Call after any albums/song_album write."""
    global _catalog_generation
    with _catalog_lock:
        _catalog_generation += 1
        _catalog_cache.clear()


def _catalog_cached(key, load):
    """The cached value for `key`, loading it with `load()` on a miss.

    Returns the cached object itself; public callers hand out a copy so a caller
    that sorts or shuffles its result cannot corrupt what the next one sees.
    """
    now = time.monotonic()
    with _catalog_lock:
        hit = _catalog_cache.get(key)
        if hit is not None and now < hit[0]:
            return hit[1]
        generation = _catalog_generation
    value = load()
    with _catalog_lock:
        if generation == _catalog_generation:
            _catalog_cache[key] = (now + CATALOG_CACHE_TTL, value)
    return value


# Rows per multi-row INSERT. SQLite builds before 3.32 cap a statement at 999
# bound values, and the widest row here has 5 columns.
_INSERT_CHUNK = 150


def _insert_rows(c, head, rows):
    """INSERT OR IGNORE `rows` in multi-row statements. Returns rows inserted.

    `head` is everything up to VALUES, e.g. "INSERT OR IGNORE INTO t (a, b)".
    On Postgres the translation layer turns OR IGNORE into ON CONFLICT DO
    NOTHING, and rowcount counts only the rows that actually went in.
    """
    inserted = 0
    for start in range(0, len(rows), _INSERT_CHUNK):
        chunk = rows[start:start + _INSERT_CHUNK]
        row_sql = "(" + ", ".join("?" * len(chunk[0])) + ")"
        c.execute(f"{head} VALUES " + ", ".join([row_sql] * len(chunk)),
                  tuple(v for row in chunk for v in row))
        inserted += max(c.rowcount, 0)
    return inserted


def save_artist_catalog(artist, artist_id, albums, tracks):
    """Write one artist import -- albums, songs and their links -- in one
    transaction.

        albums: [(album_id, name, image)]
        tracks: [(spotify_track_id, song_name, album_id, album_name)]

    Returns {"new_albums", "new_songs", "new_links"} -- what was not already
    there -- plus "unmatched", tracks that could not be linked to a song (see
    below; in practice always 0). The import used to write a row at a time -- an album, then per
    track a song insert (plus a lookup when it already existed) and a link --
    which for a large discography was well over a thousand round trips to
    the database. This is a handful of multi-row statements, and a failure
    part-way leaves nothing behind instead of half an artist.

    A song name shared by several tracks (the album cut and the single) is one
    song linked to each of them, as before. New songs start unassigned: an
    admin gives them a tier, and nothing unassigned can drop until then.
    """
    names = list(dict.fromkeys(name for _, name, _, _ in tracks))
    conn = get_connection()
    c = conn.cursor()
    try:
        _begin(conn)
        new_albums = _insert_rows(
            c, "INSERT OR IGNORE INTO albums (id, name, artist, artist_id, image)",
            [(album_id, name, artist, artist_id, image) for album_id, name, image in albums])

        new_songs = _insert_rows(
            c, "INSERT OR IGNORE INTO songs (id, name, artist, rarity)",
            [(uuid.uuid4().hex[:8], name, artist, UNASSIGNED) for name in names])

        # The ids of every song these tracks belong to, new or already there.
        # Matched exactly in Python: songs are unique on the exact (name,
        # artist) text, while on Postgres the artist comparison here is
        # case-insensitive and can bring back a differently-cased namesake.
        c.execute("SELECT id, name, artist FROM songs WHERE artist = ?", (artist,))
        song_ids = {(name, owner): song_id for song_id, name, owner in c.fetchall()}

        links, unmatched = [], 0
        for track_id, name, album_id, album_name in tracks:
            song_id = song_ids.get((name, artist))
            if song_id is None:
                # Only reachable if a freshly drawn 8-hex id collided with an
                # existing song's, which dropped that song's insert.
                unmatched += 1
                continue
            links.append((song_id, track_id, album_id, album_name))
        new_links = _insert_rows(
            c, "INSERT OR IGNORE INTO song_album (song_id, spotify_song_id, album_id, album_name)",
            links)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _invalidate_catalog()
    return {"new_albums": new_albums, "new_songs": new_songs,
            "new_links": new_links, "unmatched": unmatched}


def get_all_artists():
    """Get all unique artists from the database. Cached; see _catalog_cached."""
    def load():
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT DISTINCT artist FROM albums ORDER BY artist")
        results = c.fetchall()
        conn.close()
        return [row[0] for row in results]
    return list(_catalog_cached("artists", load))  # List of artist names


def find_artist(name):
    """The catalogue's own spelling of `name`, matched case-insensitively, or None.

    Four admin commands each carried their own copy of this loop. It reads the
    cached artist list, so resolving a name costs no query.
    """
    wanted = name.lower()
    return next((a for a in get_all_artists() if a.lower() == wanted), None)

def get_album_song_counts(artist):
    """[(album_name, song_count)] for one artist, fullest release first.

    Deliberately narrow: album autocomplete fires on every keystroke, and
    pulling one artist's whole song list to count albums in Python would mean
    shipping 400+ rows per character for a back catalogue like Taylor Swift's.
    """
    def load():
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
    # Keyed case-insensitively, matching the COLLATE NOCASE lookup: "taylor
    # swift" and "Taylor Swift" are the same query and share an entry.
    return list(_catalog_cached(("album_counts", (artist or "").lower()), load))


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

    Cached -- this backs /artists and the web catalog, and only changes when an
    artist is imported or removed.

    The counts are pre-aggregated per table and then joined. The obvious form,
    COUNT(DISTINCT ...) over albums LEFT JOIN song_album, makes Postgres sort the
    whole join to de-duplicate it (27ms measured); counting distinct
    (artist, song) pairs first lets it hash instead (8ms), with identical output.

    The covers query keeps only the first four distinct images per artist in the
    database, where it used to ship every album (527 rows) and trim in Python.
    ROW_NUMBER over MIN(name) reproduces the old order exactly: the first time an
    image appears in (artist, name) order is at its alphabetically-first album.
    Both statements are standard SQL that SQLite 3.25+ runs unchanged.
    """
    def load():
        conn = get_connection()
        c = conn.cursor()

        c.execute("""
            SELECT a.artist, a.album_count, COALESCE(s.song_count, 0)
            FROM (SELECT artist, COUNT(*) AS album_count FROM albums GROUP BY artist) a
            LEFT JOIN (
                SELECT artist, COUNT(*) AS song_count
                FROM (SELECT DISTINCT albums.artist AS artist, song_album.song_id
                      FROM song_album JOIN albums ON albums.id = song_album.album_id) d
                GROUP BY artist
            ) s ON s.artist = a.artist
            ORDER BY a.artist
        """)
        counts = c.fetchall()

        c.execute("""
            SELECT artist, image FROM (
                SELECT artist, image,
                       ROW_NUMBER() OVER (PARTITION BY artist ORDER BY MIN(name), image) AS rn
                FROM albums
                WHERE image IS NOT NULL AND image <> ''
                GROUP BY artist, image
            ) t
            WHERE rn <= 4
            ORDER BY artist, rn
        """)
        covers = {}
        for artist, image in c.fetchall():
            covers.setdefault(artist, []).append(image)

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

    return [dict(entry, covers=list(entry["covers"]))
            for entry in _catalog_cached("overview", load)]

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

    The artist list comes from the catalogue cache, so only the pull counts --
    which change on every pull -- cost a round trip.
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
    conn.close()

    artists = get_all_artists()
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
    # This used to return without closing, leaving the connection to be handed
    # back whenever the garbage collector got round to it.
    conn.close()
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
    """Add a card. A mythic goes through claim_mythic instead, since it needs a
    copy number and has a cap -- its (status, copy_number) is returned."""
    if variant == "mythic":
        return claim_mythic(user_id, song_id, album_id)
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO collections (user_id, song_id, album_id, variant)
        VALUES (?, ?, ?, ?)
    """, (user_id, song_id, album_id, variant))
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

def get_user_tradeable_items(user_id, rarity=None, artist=None):
    """A user's collection rows for trade selection and autocomplete.

    Both filters are optional and narrow independently: `rarity` is the
    collection *variant*, `artist` the song's artist. Passing neither returns
    the whole collection, which is what the trade autocompletes want before the
    player has chosen anything.

    Column order (shared with get_collection_item_by_id):
    (collection_id, song_id, album_id, song_name, artist_name, variant,
     album_name, rarity, copy_number) -- copy_number is None except on mythics."""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    sql = """
        SELECT collections.id, songs.id, albums.id, songs.name, songs.artist, collections.variant, albums.name, songs.rarity,
               collections.copy_number
        FROM collections
        JOIN songs ON collections.song_id = songs.id
        JOIN albums ON collections.album_id = albums.id
        WHERE collections.user_id = ?
    """
    params = [user_id]
    if rarity:
        sql += " AND collections.variant = ?"
        params.append(rarity)
    if artist:
        sql += " AND songs.artist = ? COLLATE NOCASE"
        params.append(artist)
    sql += " ORDER BY songs.artist ASC, songs.name ASC"
    c.execute(sql, tuple(params))
    results = c.fetchall()
    conn.close()
    return results

def get_collection_item_by_id(collection_id, user_id):
    """Get a single collection row by its id, scoped to a user (ownership check).
    Same column order as get_user_tradeable_items."""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT collections.id, songs.id, albums.id, songs.name, songs.artist, collections.variant, albums.name, songs.rarity,
               collections.copy_number
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

    Reassigns the existing row rather than deleting and re-inserting it, so the
    card keeps its id, its pull date and -- for a mythic -- the copy number
    stored on it.

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


def swap_collection_items(first_id, first_owner, second_id, second_owner):
    """Complete a trade: each card goes to the other owner, or neither moves.

    The two moves used to be separate calls, so a trade whose second card had
    been gifted away in the meantime still handed over the first one and then
    reported failure -- a one-sided trade with nothing to undo it. Both rows
    now move in one statement inside a transaction, each guarded by its
    current owner, and anything short of both moving is rolled back.
    """
    first_owner, second_owner = _uid(first_owner), _uid(second_owner)
    conn = get_connection()
    c = conn.cursor()
    try:
        _begin(conn)
        c.execute("""
            UPDATE collections SET user_id = CASE WHEN id = ? THEN ? ELSE ? END
            WHERE (id = ? AND user_id = ?) OR (id = ? AND user_id = ?)
        """, (first_id, second_owner, first_owner,
              first_id, first_owner, second_id, second_owner))
        if c.rowcount != 2:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def register_user(user_id, xp=0, username=None, sig_vinyl_count=0):
    """Create an account. True if this call created it, False if it existed.

    A welcome gift is passed as `sig_vinyl_count` so it lands in the same
    INSERT: it is granted exactly when the account is created, and two
    /register calls sent together cannot both see "not registered" and both
    hand it out.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (id, xp, username, sig_vinyl_count) VALUES (?, ?, ?, ?)",
              (user_id, xp, username, sig_vinyl_count))
    created = c.rowcount > 0
    conn.commit()
    conn.close()
    return created

def get_song_and_album_details(song_name, album_name, artist):
    """Get song_id, album_id, and album_url by song name, album name, and artist.

    One statement: the album lookup is LEFT JOINed onto the song lookup, so a
    missing song gives no row (all None) and a found song with a missing album
    still returns its id and name, exactly as the old two-query form did.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT s.id, s.name, a.id, a.name, a.image
        FROM (SELECT id, name FROM songs
              WHERE name = ? COLLATE NOCASE AND artist = ? COLLATE NOCASE
              LIMIT 1) s
        LEFT JOIN (SELECT id, name, image FROM albums
                   WHERE name = ? COLLATE NOCASE AND artist = ? COLLATE NOCASE
                   LIMIT 1) a ON 1 = 1
    """, (song_name, artist, album_name, artist))
    row = c.fetchone()
    conn.close()

    if not row:
        return None, None, None, None, None  # Song not found
    song_id, song_name, album_id, album_name, album_url = row
    if album_id is None:
        return song_id, song_name, None, None, None  # Album not found

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

    The singles and specific-album forms return (id, name, album, track_count,
    rarity). The rarity rides along so /list does not look it up once per song.
    """
    conn = get_connection()
    c = conn.cursor()
    
    if album_name == "singles":
        # Get songs from albums with 3 or fewer tracks (singles)
        # Exclude songs that also appear on albums/EPs (4+ tracks)
        c.execute("""
            SELECT songs.id, songs.name, albums.name as album_name,
                   COUNT(song_album.song_id) OVER (PARTITION BY albums.id) as track_count,
                   songs.rarity
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
                   COUNT(song_album.song_id) OVER (PARTITION BY albums.id) as track_count,
                   songs.rarity
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

# Tiers `.assign 0` leaves alone: it only fills in songs that have none yet.
_PRESERVED_TIERS = ("ultimate", "legendary", "elite", "unique")


def assign_basic_bulk(song_ids):
    """`.assign 0`: set every listed song without a higher tier to basic.

    Returns (assigned, missing): how many songs were set, and which ids no
    longer exist. One UPDATE for the whole list -- this used to be three
    round trips per song (find it by name, read its rarity, write it), ~60
    for a 20-track album.
    """
    ids = list(dict.fromkeys(song_ids))
    if not ids:
        return 0, []
    placeholders = ",".join("?" * len(ids))
    tiers = ",".join("?" * len(_PRESERVED_TIERS))
    conn = get_connection()
    c = conn.cursor()
    c.execute(f"SELECT id FROM songs WHERE id IN ({placeholders})", tuple(ids))
    present = {row[0] for row in c.fetchall()}
    c.execute(f"""
        UPDATE songs SET rarity = 'basic'
        WHERE id IN ({placeholders})
          AND (rarity IS NULL OR LOWER(rarity) NOT IN ({tiers}))
    """, (*ids, *_PRESERVED_TIERS))
    assigned = c.rowcount
    conn.commit()
    conn.close()
    return assigned, [song_id for song_id in ids if song_id not in present]


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
    """Remove all songs, albums, and song_album entries for a given artist.

    All-or-nothing: the deletes run in one explicit transaction, because a
    failure halfway would otherwise leave songs with no albums or collection
    rows pointing at nothing. The id lists that used to be fetched first are
    subqueries now, which saves two round trips and cannot go stale -- nothing
    is removed from songs or albums until the rows referencing them are gone.
    """
    conn = get_connection()
    c = conn.cursor()

    try:
        _begin(conn)
        c.execute("""
            DELETE FROM song_album
            WHERE song_id IN (SELECT id FROM songs WHERE artist = ? COLLATE NOCASE)
        """, (artist_name,))
        song_album_deletions = c.rowcount

        # FK enforcement is on, so any collection rows referencing these songs
        # or albums must go before the parent rows can be deleted.
        c.execute("""
            DELETE FROM collections
            WHERE song_id IN (SELECT id FROM songs WHERE artist = ? COLLATE NOCASE)
               OR album_id IN (SELECT id FROM albums WHERE artist = ? COLLATE NOCASE)
        """, (artist_name, artist_name))

        c.execute("DELETE FROM songs WHERE artist = ? COLLATE NOCASE", (artist_name,))
        song_deletions = c.rowcount

        c.execute("DELETE FROM albums WHERE artist = ? COLLATE NOCASE", (artist_name,))
        album_deletions = c.rowcount

        conn.commit()
        _invalidate_catalog()

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


def _case_by_id(values):
    """`CASE id WHEN ? THEN ? ... END` and its parameters, for per-user
    amounts in one UPDATE."""
    return ("CASE id " + " ".join("WHEN ? THEN ?" for _ in values) + " END",
            [v for pair in values.items() for v in pair])


def add_user_xp_many(amounts):
    """Add XP to one or more players in one statement.

    `amounts` is {user_id: xp}; returns {user_id: (xp_before, xp_after)} with
    the caller's own keys, leaving out anyone who is not registered.

    Both numbers come out of the UPDATE itself (RETURNING), so a level-up is
    judged on exactly this grant. Reading the XP first and adding it after,
    as every caller used to, could see a second grant for the same player
    land in between and pay the same level twice. A song battle's XP for all
    its players, a round trip and more each before, is one statement too.
    """
    keys = {_uid(user_id): user_id for user_id in amounts}
    gains = {_uid(user_id): int(round(xp)) for user_id, xp in amounts.items()}
    if not gains:
        return {}
    case, case_params = _case_by_id(gains)
    conn = get_connection()
    c = conn.cursor()
    c.execute(f"""
        UPDATE users SET xp = COALESCE(xp, 0) + {case}
        WHERE id IN ({",".join("?" * len(gains))})
        RETURNING id, xp
    """, (*case_params, *gains))
    after = dict(c.fetchall())
    conn.commit()
    conn.close()
    return {keys[user_id]: (after[user_id] - gains[user_id], after[user_id])
            for user_id in gains if user_id in after}


def add_vinyls_many(amounts):
    """Add vinyl and signature vinyl pulls for one or more players in one
    statement -- level-up rewards, or an admin grant.

    `amounts` is {user_id: (vinyls, sig_vinyls)}; returns {user_id:
    (vinyl_count, sig_vinyl_count)} as they stand afterwards, which is what
    every caller goes on to show, so no second read is needed. Players who
    are not registered are left out.
    """
    keys = {_uid(user_id): user_id for user_id in amounts}
    if not keys:
        return {}
    vinyls, vinyl_params = _case_by_id({_uid(u): v for u, (v, _) in amounts.items()})
    sigs, sig_params = _case_by_id({_uid(u): s for u, (_, s) in amounts.items()})
    conn = get_connection()
    c = conn.cursor()
    c.execute(f"""
        UPDATE users
        SET vinyl_count = COALESCE(vinyl_count, 0) + {vinyls},
            sig_vinyl_count = COALESCE(sig_vinyl_count, 0) + {sigs}
        WHERE id IN ({",".join("?" * len(keys))})
        RETURNING id, vinyl_count, sig_vinyl_count
    """, (*vinyl_params, *sig_params, *keys))
    after = {user_id: (vinyl or 0, sig or 0) for user_id, vinyl, sig in c.fetchall()}
    conn.commit()
    conn.close()
    return {keys[user_id]: after[user_id] for user_id in keys if user_id in after}


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


DAILY_COOLDOWN = _datetime.timedelta(hours=24)


def claim_daily(user_id, xp):
    """Pay the daily reward if it is due, in one statement.

    The check (can_claim_daily) and the payout used to be separate calls, so
    two `.daily`s sent together could both pass the check before either wrote
    the new claim time, and both pay out. The 24-hour test now sits in the
    UPDATE's WHERE clause, against the same naive-UTC clock can_claim_daily
    reads.

    Returns ("claimed", (xp_before, xp_after)), ("wait", seconds_left) or
    ("unregistered", None).
    """
    user_id = _uid(user_id)
    cutoff = _datetime.datetime.now(_datetime.timezone.utc).replace(tzinfo=None) - DAILY_COOLDOWN
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE users SET xp = COALESCE(xp, 0) + ?, last_daily_claim = CURRENT_TIMESTAMP
        WHERE id = ? AND (last_daily_claim IS NULL OR last_daily_claim <= ?)
        RETURNING xp
    """, (xp, user_id, cutoff))
    row = next(iter(c.fetchall()), None)
    conn.commit()
    conn.close()
    if row:
        return "claimed", (row[0] - xp, row[0])
    if not user_exists(user_id):
        return "unregistered", None
    return "wait", can_claim_daily(user_id)[1]

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
#
# A copy's number lives in collections.copy_number, written once when the copy
# is claimed; everything below reads that column rather than working the
# number out from claim order.

# The number the next copy of a song gets: the lowest one not in use. With no
# gaps that is simply count + 1. A copy an admin removes frees its number for
# the next claim, so the numbers stay within 1..cap and never shift -- the old
# computed numbers renumbered every later copy whenever one was deleted.
# Binds the song id twice.
_NEXT_COPY_SQL = """
    CASE WHEN NOT EXISTS (SELECT 1 FROM collections f
                          WHERE f.song_id = ? AND f.variant = 'mythic' AND f.copy_number = 1)
         THEN 1
         ELSE (SELECT MIN(g.copy_number) + 1 FROM collections g
               WHERE g.song_id = ? AND g.variant = 'mythic' AND g.copy_number IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM collections h
                                 WHERE h.song_id = g.song_id AND h.variant = 'mythic'
                                   AND h.copy_number = g.copy_number + 1))
    END"""


def claim_mythic(user_id, song_id, album_id, max_copies=None):
    """Put a mythic copy in a collection and number it, in one statement.

    The cap, the one-copy-per-player rule and the number are all settled inside
    the INSERT, so no check made a moment earlier can go stale before the write.
    Two claims for one song landing together compute the same number; the
    unique index refuses the second, which retries and then sees the first.

    Returns ("claimed", copy_number), ("owned", None) or ("full", None).
    """
    max_copies = odds.MYTHIC_MAX_COPIES if max_copies is None else max_copies
    user_id = _uid(user_id)
    conn = get_connection()
    try:
        c = conn.cursor()
        for attempt in range(3):
            try:
                c.execute(f"""
                    INSERT INTO collections (user_id, song_id, album_id, variant, copy_number)
                    SELECT ?, ?, ?, 'mythic', {_NEXT_COPY_SQL}
                    WHERE (SELECT COUNT(*) FROM collections
                           WHERE song_id = ? AND variant = 'mythic') < ?
                      AND NOT EXISTS (SELECT 1 FROM collections
                                      WHERE song_id = ? AND variant = 'mythic' AND user_id = ?)
                    RETURNING copy_number
                """, (user_id, song_id, album_id, song_id, song_id,
                      song_id, max_copies, song_id, user_id))
                # fetchall, not fetchone: SQLite finishes a RETURNING statement
                # only once it is stepped to the end, and cannot commit before.
                row = next(iter(c.fetchall()), None)
                conn.commit()
                break
            except Exception as exc:
                if attempt == 2 or not _is_unique_violation(exc):
                    raise
                conn.rollback()
        if row:
            return "claimed", row[0]
        c.execute("SELECT 1 FROM collections WHERE song_id = ? AND variant = 'mythic' AND user_id = ?",
                  (song_id, user_id))
        return ("owned" if c.fetchone() else "full"), None
    finally:
        conn.close()


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

    'next_copy_number' is the one claim_mythic would hand out right now, so the
    reveal shows the number the claim will actually give.

    Returns the same keys as get_mythic_copy_info(), plus 'rarity' and
    'user_owns'.
    """
    max_copies = odds.MYTHIC_MAX_COPIES if max_copies is None else max_copies
    user_id = _uid(user_id)

    conn = get_connection()
    c = conn.cursor()
    c.execute(f"""
        SELECT (SELECT rarity FROM songs WHERE id = ?),
               COUNT(*),
               COUNT(CASE WHEN user_id = ? THEN 1 END),
               {_NEXT_COPY_SQL}
        FROM collections
        WHERE song_id = ? AND variant = 'mythic'
    """, (song_id, user_id, song_id, song_id, song_id))
    row = c.fetchone()
    conn.close()

    rarity, current_copies, mine, next_copy = (row or (None, 0, 0, 1))
    current_copies = current_copies or 0
    return {
        'rarity': rarity,
        'max_copies': max_copies,
        'current_copies': current_copies,
        'remaining_copies': max_copies - current_copies,
        'can_collect': current_copies < max_copies,
        'next_copy_number': next_copy,
        'user_owns': bool(mine),
    }


def get_mythic_copy_info(song_id, max_copies=None):
    """How many copies of a mythic are out, and the number the next one gets."""
    max_copies = odds.MYTHIC_MAX_COPIES if max_copies is None else max_copies
    conn = get_connection()
    c = conn.cursor()
    c.execute(f"""
        SELECT (SELECT COUNT(*) FROM collections WHERE song_id = ? AND variant = 'mythic'),
               {_NEXT_COPY_SQL}
    """, (song_id, song_id, song_id))
    current_copies, next_copy = c.fetchone()
    conn.close()
    return {
        'max_copies': max_copies,
        'current_copies': current_copies,
        'remaining_copies': max_copies - current_copies,
        'can_collect': current_copies < max_copies,
        'next_copy_number': next_copy,
    }


def get_mythic_owners(song_id):
    """Who holds each copy of a mythic: [(user_id, collected_at, copy_number)],
    in copy order."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT user_id, collected_at, copy_number
        FROM collections
        WHERE song_id = ? AND variant = 'mythic'
        ORDER BY copy_number ASC, collected_at ASC, id ASC
    """, (song_id,))
    results = c.fetchall()
    conn.close()
    return results

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
            CASE WHEN collections.variant = 'mythic' THEN collections.copy_number ELSE 1 END,
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

def get_available_mythic_songs(max_copies=None):
    """Songs that can still be collected as mythics (fewer than the cap exist).

    The cap comes from odds.MYTHIC_MAX_COPIES, like every other mythic check.
    This used to hardcode 3 from when that was the cap, so once it rose to 5 a
    song with 3 or 4 copies was collectable everywhere except here.

    NOT EXISTS rather than NOT IN: NOT IN yields no rows at all if its subquery
    ever produces a NULL, and it is no faster.
    """
    max_copies = odds.MYTHIC_MAX_COPIES if max_copies is None else max_copies
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT s.id, s.name, s.artist
        FROM songs s
        WHERE NOT EXISTS (
            SELECT 1 FROM collections
            WHERE song_id = s.id AND variant = 'mythic'
            GROUP BY song_id
            HAVING COUNT(*) >= ?
        )
    """, (max_copies,))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_id, song_name, artist_name)

def get_available_mythic_songs_for_user(user_id, limit=None, max_copies=None):
    """Songs this user can still be given as a mythic: under the copy cap, and
    not one they already own.

    `limit` picks that many at random in the database. The mythic swap only ever
    wanted one, but used to receive the whole eligible catalogue (~1,800 rows)
    and random.choice it in Python -- ~85ms of transfer for a single song. Each
    row is equally likely either way. Leave it None for the full list.

    See get_available_mythic_songs for the cap and NOT EXISTS reasoning.
    """
    user_id = _uid(user_id)
    max_copies = odds.MYTHIC_MAX_COPIES if max_copies is None else max_copies
    sql = """
        SELECT s.id, s.name, s.artist
        FROM songs s
        WHERE NOT EXISTS (
            SELECT 1 FROM collections
            WHERE song_id = s.id AND variant = 'mythic'
            GROUP BY song_id
            HAVING COUNT(*) >= ?
        )
        AND NOT EXISTS (
            SELECT 1 FROM collections
            WHERE song_id = s.id AND user_id = ? AND variant = 'mythic'
        )
    """
    params = [max_copies, user_id]
    if limit is not None:
        sql += " ORDER BY RANDOM() LIMIT ?"
        params.append(int(limit))
    conn = get_connection()
    c = conn.cursor()
    c.execute(sql, tuple(params))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_id, song_name, artist_name)

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

def get_vinyl_counts(user_id):
    """(vinyl_count, sig_vinyl_count) in one read, or None if not registered."""
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COALESCE(vinyl_count, 0), COALESCE(sig_vinyl_count, 0) FROM users WHERE id = ?",
              (user_id,))
    row = c.fetchone()
    conn.close()
    return tuple(row) if row else None


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
    """Use one vinyl pull from user's count. Returns True if successful, False if no pulls left.

    One guarded UPDATE. This used to read the count first through
    get_vinyl_count(), which opened a *second* pooled connection while
    this one was still held, then updated anyway: the `vinyl_count > 0` guard
    already refuses an empty balance, and rowcount says which happened. A NULL
    count fails the guard too, the same answer the COALESCE'd read gave.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
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
    """Use one signature vinyl pull from user's count. Returns True if successful, False if no pulls left.

    One guarded UPDATE. This used to read the count first through
    get_sig_vinyl_count(), which opened a *second* pooled connection while
    this one was still held, then updated anyway: the `sig_vinyl_count > 0` guard
    already refuses an empty balance, and rowcount says which happened. A NULL
    count fails the guard too, the same answer the COALESCE'd read gave.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
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
    """Remove the latest n songs added to the collection across all users.

    One DELETE with the selection as a subquery, so the rows chosen and the rows
    removed cannot differ -- a pull landing between a separate SELECT and DELETE
    used to be able to shift which ones "the latest" meant.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        DELETE FROM collections
        WHERE id IN (SELECT id FROM collections
                     ORDER BY collected_at DESC
                     LIMIT ?)
    """, (n,))
    removed = c.rowcount
    conn.commit()
    conn.close()
    return removed  # Number of items

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
    with where they appear in the list. The user's own XP is read in the same
    statement, so no row at all means they are not registered.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT 1 + (SELECT COUNT(*) FROM users ahead
                    WHERE COALESCE(ahead.xp, 0) > COALESCE(me.xp, 0)
                       OR (COALESCE(ahead.xp, 0) = COALESCE(me.xp, 0) AND ahead.id < me.id))
        FROM users me
        WHERE me.id = ?
    """, (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


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


def take_mythic_hunt():
    """Read and clear the hunt in one statement, for the pull it goes to.

    The pull used to read the hunt and then clear it as two calls, so two
    mythic pulls landing together could both read it and both be handed the
    hunted song. Deleting the row reads it at the same time; get_mythic_hunt
    treats a missing row exactly like a cleared one.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        DELETE FROM mythic_state WHERE id = 1 AND song_id IS NOT NULL
        RETURNING song_id, song_name, album_id, album_name, album_url, artist
    """)
    row = next(iter(c.fetchall()), None)
    conn.commit()
    conn.close()
    if not row:
        return None
    return dict(zip(("song_id", "song_name", "album_id", "album_name", "album_url", "artist"), row))


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
    collections.collected_at, collections.copy_number
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
        # Stored on the row since the column was added; None unless mythic.
        "copy_number": row[11],
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


def get_card_screen_stats(collection_id, song_id, owner_id=None):
    """(owners, pinned) for one card screen, in a single query.

    owners counts every copy of the song, and pinned says whether this is the
    owner's pinned card. The copy number used to be worked out here too; it
    now rides on the card itself (see _card_dict). `pinned` is None when
    owner_id is not given, for callers that already know.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT (SELECT COUNT(*) FROM collections WHERE song_id = ?),
               (SELECT pinned_collection_id FROM users WHERE id = ?)
    """, (song_id, _uid(owner_id) if owner_id is not None else None))
    owners, pinned_id = c.fetchone()
    conn.close()
    pinned = None if owner_id is None else (pinned_id is not None and pinned_id == collection_id)
    return owners or 0, pinned


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
    """Everything /profile needs, in one statement.

    This used to be five queries -- the user row, then four separate passes over
    the same collection -- which with the transaction wrapper cost seven round
    trips. Each aggregate is now a derived table cross-joined onto the user row
    (`ON 1 = 1`, which SQLite and Postgres both accept), so the database does the
    same four scans and the network carries one request.

    The result has one row per rarity the user holds. A user with no cards still
    gets exactly one row, with the rarity columns NULL -- told apart from a real
    NULL-rarity group by its count being NULL rather than a number.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT u.username, u.xp, u.vinyl_count, u.sig_vinyl_count,
               u.pinned_collection_id, u.favorite_artist,
               t.cards, t.unique_songs, t.mythics,
               ta.artist, ta.n,
               s.since,
               r.rarity, r.n
        FROM users u
        LEFT JOIN (
            SELECT COUNT(*) AS cards,
                   COUNT(DISTINCT collections.song_id) AS unique_songs,
                   SUM(CASE WHEN collections.variant = 'mythic' THEN 1 ELSE 0 END) AS mythics
            FROM collections
            JOIN songs ON collections.song_id = songs.id
            WHERE collections.user_id = ?
        ) t ON 1 = 1
        LEFT JOIN (
            -- Most-owned artist: earned, as opposed to the chosen favorite.
            SELECT songs.artist AS artist, COUNT(*) AS n
            FROM collections
            JOIN songs ON collections.song_id = songs.id
            WHERE collections.user_id = ?
            GROUP BY songs.artist
            ORDER BY n DESC, songs.artist ASC
            LIMIT 1
        ) ta ON 1 = 1
        LEFT JOIN (
            SELECT MIN(collected_at) AS since FROM collections WHERE user_id = ?
        ) s ON 1 = 1
        LEFT JOIN (
            SELECT songs.rarity AS rarity, COUNT(*) AS n
            FROM collections
            JOIN songs ON collections.song_id = songs.id
            WHERE collections.user_id = ?
            GROUP BY songs.rarity
        ) r ON 1 = 1
        WHERE u.id = ?
    """, (user_id, user_id, user_id, user_id, user_id))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return None

    (username, xp, vinyl, sig_vinyl, pinned_id, favorite,
     cards, unique_songs, mythics, top_artist, top_count, since, _, _) = rows[0]
    rarity_counts = {rarity: n for *_, rarity, n in rows if n is not None}

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
        "top_artist": top_artist,
        "top_artist_count": top_count or 0,
        "collecting_since": since,
    }


def get_artist_completion(user_id, artist):
    """How much of one artist's catalog a user holds: (owned, total).

    Both counts as scalar subqueries of one SELECT, so this is a single round
    trip rather than two.
    """
    user_id = _uid(user_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT (SELECT COUNT(DISTINCT collections.song_id)
                FROM collections
                JOIN songs ON collections.song_id = songs.id
                WHERE collections.user_id = ? AND songs.artist = ? COLLATE NOCASE),
               (SELECT COUNT(*) FROM songs WHERE artist = ? COLLATE NOCASE)
    """, (user_id, artist, artist))
    owned, total = c.fetchone() or (0, 0)
    conn.close()
    return owned or 0, total or 0


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
