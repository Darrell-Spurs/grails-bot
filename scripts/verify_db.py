"""Verify the configured database backend end to end.

Run it against SQLite (no DATABASE_URL) or against Postgres/Supabase
(DATABASE_URL set). It creates the schema, writes and reads a throwaway row,
exercises the real query functions, then cleans up after itself.

    python scripts/verify_db.py
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

import db  # noqa: E402  (must come after load_dotenv: db reads DATABASE_URL on import)

TEST_USER = "__verify_probe_user__"
TEST_SONG = "__verify_probe_song__"
TEST_ALBUM = "__verify_probe_album__"

failures = []


def check(label, fn):
    try:
        fn()
        print(f"  PASS  {label}")
    except Exception as e:
        print(f"  FAIL  {label}: {type(e).__name__}: {e}")
        failures.append(label)


def main():
    backend = "Postgres" if db.IS_POSTGRES else f"SQLite ({db.DB_FILE})"
    print(f"Backend: {backend}\n")

    print("--- schema ---")
    check("init_db() creates/upgrades the schema", db.init_db)

    print("\n--- catalog reads ---")
    check("get_all_artists()", lambda: db.get_all_artists())
    check("search_songs()", lambda: db.search_songs("a", limit=5))
    check("get_mythic_hunt()", lambda: db.get_mythic_hunt())

    print("\n--- write/read round trip ---")

    def seed():
        db.add_song(TEST_SONG, "Probe Song", "Probe Artist", "basic")
        db.add_album(TEST_ALBUM, "Probe Album", "Probe Artist", "probe", "http://example/x.png")
        # The artist/rarity lookup joins through song_album, so link them.
        db.link_song_album(TEST_SONG, TEST_SONG + "_sp", TEST_ALBUM, "Probe Album")
        db.register_user(TEST_USER, xp=0, username="probe")

    check("insert probe song/album/user", seed)
    check("add_song_to_collection()",
          lambda: db.add_song_to_collection(TEST_USER, TEST_SONG, TEST_ALBUM, "default"))
    check("user_exists() sees the probe user", lambda: _assert(db.user_exists(TEST_USER)))
    check("add_user_xp()", lambda: db.add_user_xp(TEST_USER, 5))
    check("get_user_xp() reflects the write", lambda: _assert(db.get_user_xp(TEST_USER) >= 5))
    check("get_collection_with_copy_numbers()",
          lambda: _assert(len(db.get_collection_with_copy_numbers(TEST_USER)) >= 1))
    check("get_user_tradeable_items()",
          lambda: _assert(len(db.get_user_tradeable_items(TEST_USER, "default")) >= 1))

    print("\n--- case-insensitive lookup (COLLATE NOCASE / CITEXT) ---")
    check("lowercase artist still matches",
          lambda: _assert(len(db.get_songs_by_artist_and_rarity("probe artist", "basic")) >= 1))

    print("\n--- cleanup ---")
    check("remove probe rows", cleanup)

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILED -> {', '.join(failures)}")
        return 1
    print("RESULT: ALL PASS - this backend is good to go.")
    return 0


def _assert(cond):
    if not cond:
        raise AssertionError("expected a truthy result")


def cleanup():
    conn = db.get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM collections WHERE user_id = ?", (TEST_USER,))
        c.execute("DELETE FROM users WHERE id = ?", (TEST_USER,))
        c.execute("DELETE FROM song_album WHERE song_id = ?", (TEST_SONG,))
        c.execute("DELETE FROM songs WHERE id = ?", (TEST_SONG,))
        c.execute("DELETE FROM albums WHERE id = ?", (TEST_ALBUM,))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
