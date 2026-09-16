"""Copy the song catalog from local SQLite into the Postgres database.

Copies only catalog tables (songs, albums, song_album). Player data -- users,
collections, mythic_state -- is deliberately NOT copied, so the new server starts
with a clean slate while keeping every song, album and rarity you've curated.

Usage:
    # DATABASE_URL must point at the target Postgres database
    python scripts/migrate_catalog.py --dry-run    # show what would be copied
    python scripts/migrate_catalog.py             # actually copy
"""
import argparse
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

import db  # noqa: E402

# (table, columns, primary key) in dependency order -- songs/albums before the
# link table. The key column is what the post-copy verification diffs on.
CATALOG = [
    ("songs", ["id", "name", "artist", "rarity"], "id"),
    ("albums", ["id", "name", "artist", "artist_id", "image"], "id"),
    ("song_album", ["song_id", "spotify_song_id", "album_name", "album_id"], "spotify_song_id"),
]

BATCH = 500


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report counts without writing")
    ap.add_argument("--sqlite", default=None, help="source SQLite file (defaults to the project's music.db)")
    args = ap.parse_args()

    if not db.IS_POSTGRES:
        print("DATABASE_URL is not set to a Postgres URL - nothing to migrate into.")
        print("Set DATABASE_URL in .env first, then re-run.")
        return 1

    source_path = args.sqlite or os.path.join(PROJECT_ROOT, "music.db")
    if not os.path.exists(source_path):
        print(f"Source SQLite file not found: {source_path}")
        return 1

    src = sqlite3.connect(source_path)
    print(f"Source: {source_path}")
    print(f"Target: Postgres (DATABASE_URL)\n")

    print("Ensuring target schema exists...")
    db.init_db()

    total = 0
    for table, cols, _key in CATALOG:
        rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        print(f"{table}: {len(rows)} rows", end="")

        if args.dry_run:
            print("  (dry run, not written)")
            continue

        placeholders = ", ".join(["?"] * len(cols))
        # INSERT OR IGNORE is rewritten to ON CONFLICT DO NOTHING for Postgres by
        # db.py's translation layer, so re-running this script is safe.
        sql = f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"

        conn = db.get_connection()
        try:
            c = conn.cursor()
            for i in range(0, len(rows), BATCH):
                for row in rows[i:i + BATCH]:
                    c.execute(sql, tuple(row))
                conn.commit()
            print("  -> copied")
            total += len(rows)
        finally:
            conn.close()

    if args.dry_run:
        src.close()
        print("\nDry run complete. Re-run without --dry-run to copy.")
        return 0

    # INSERT OR IGNORE / ON CONFLICT DO NOTHING drops rows silently, so the copy
    # loop's own count proves nothing. Diff the two databases key by key.
    print("\nVerifying target against source...")
    mismatches = 0
    for table, _cols, key in CATALOG:
        src_keys = {r[0] for r in src.execute(f"SELECT {key} FROM {table}")}
        conn = db.get_connection()
        try:
            c = conn.cursor()
            c.execute(f"SELECT {key} FROM {table}")
            dst_keys = {r[0] for r in c.fetchall()}
        finally:
            conn.close()

        missing = src_keys - dst_keys
        extra = dst_keys - src_keys
        status = "OK" if not missing and not extra else "MISMATCH"
        print(f"  {table:12} source={len(src_keys):6}  target={len(dst_keys):6}  [{status}]")
        for label, bad in (("in source but not in target", missing),
                           ("in target but not in source", extra)):
            if bad:
                mismatches += 1
                print(f"    {len(bad)} row(s) {label}, e.g.:")
                for k in sorted(bad)[:10]:
                    print(f"      {key}={k}")

    src.close()

    if mismatches:
        print("\nMigration INCOMPLETE - see the mismatches above.")
        print("The script is safe to re-run once the cause is fixed.")
        return 1

    print(f"\nCopied {total} catalog rows; source and target match exactly.")
    print("Player tables (users, collections, mythic_state) were left empty by design.")
    print("Verify with: python scripts/verify_db.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
