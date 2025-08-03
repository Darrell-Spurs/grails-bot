import sqlite3

DB_FILE = "music.db"

def get_connection():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = get_connection()
    c = conn.cursor()

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

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT
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

    conn.commit()
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

def add_user(user_id, username):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)", (user_id, username))
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
                WHEN 'vinyl' THEN 2
                WHEN 'sketched' THEN 3
                WHEN 'glitched' THEN 4
                WHEN 'default' THEN 5
                ELSE 6
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
            WHEN 'vinyl' THEN 2
            WHEN 'sketched' THEN 3
            WHEN 'glitched' THEN 4
            WHEN 'default' THEN 5
            ELSE 6
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
            WHEN 'vinyl' THEN 2
            WHEN 'sketched' THEN 3
            WHEN 'glitched' THEN 4
            WHEN 'default' THEN 5
            ELSE 6
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
            WHEN 'vinyl' THEN 2
            WHEN 'sketched' THEN 3
            WHEN 'glitched' THEN 4
            WHEN 'default' THEN 5
            ELSE 6
        END, songs.artist ASC
    """, (user_id, rarity, artist_name))
    results = c.fetchall()
    conn.close()
    return results  # List of (song_name, artist_name, variant, album_name, album_image)

def remove_mythic_from_collection():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        DELETE FROM collections
        WHERE variant = 'mythic'
    """)
    conn.commit()
    conn.close()
