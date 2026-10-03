"""Apply a rarity JSON ({song_name: rarity}) to an artist's songs in the database.

The JSON is what the artist-import skill writes (rarities/<artist>.json). Import
the artist on the admin panel first: this only changes rarities of songs that
are already in the catalogue.

    python scripts/apply_rarity.py rarities/niall_horan.json            # preview only
    python scripts/apply_rarity.py rarities/niall_horan.json --apply    # write it
    python scripts/apply_rarity.py some.json --artist "Niall Horan" --apply

The artist comes from the file name (niall_horan.json -> Niall Horan, matched
against the catalogue case-insensitively) unless --artist is given.

Songs are matched by exact name among that artist's songs, falling back to a
case-insensitive match. Then two gaps are closed, so the artist ends up fully
assigned:

  * catalogue songs the JSON does not list (too small for kworb to track, or a
    title spelled differently) become basic -- only if they are unassigned; a
    song that already has a tier keeps it;
  * if the JSON's ultimate is not in the catalogue (usually a song the artist
    features on, released by someone else), the most-streamed song that is
    takes ultimate. The JSON lists songs most-streamed first, as kworb does.

JSON names with no song in the catalogue are listed and skipped. All changes
are written in one transaction.

Uses DATABASE_URL from .env like the bot does -- that is production when it
points at Supabase. Without --apply nothing is written.
"""
import argparse
import json
import os
import sys
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

import db  # noqa: E402  (must come after load_dotenv: db reads DATABASE_URL on import)
from utils.aesthetics import RARITY_ORDER  # noqa: E402


def load_rarities(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        sys.exit(f"{path}: expected an object of {{song_name: rarity}}")
    bad = {name: r for name, r in data.items() if str(r).lower() not in RARITY_ORDER}
    if bad:
        sys.exit("Unknown rarity for: " + ", ".join(f"{n!r} ({r})" for n, r in bad.items())
                 + f"\nAllowed: {', '.join(RARITY_ORDER)}")
    return {name: str(r).lower() for name, r in data.items()}


def resolve_artist(path, given):
    wanted = given or os.path.splitext(os.path.basename(path))[0].replace("_", " ")
    found = db.find_artist(wanted)
    if not found:
        sys.exit(f"No artist {wanted!r} in the catalogue. Import it on the panel first, "
                 f"or pass --artist with the exact name.")
    return found


def artist_songs(artist):
    """[(id, name, rarity)] for every song stored under `artist`."""
    conn = db.get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT id, name, rarity FROM songs WHERE artist = ?", (artist,))
        return [(sid, name, rarity) for sid, name, rarity in c.fetchall()
                if name is not None]
    finally:
        conn.close()


def plan(rarities, songs):
    """(changes, unchanged, missing_in_db, unlisted, promoted).

    changes: [(song_id, name, old, new)]. Every DB row with a matching name is
    covered, so case-variant duplicates of one title all get the same tier.
    unlisted: catalogue songs the JSON does not name, as (name, old, new).
    promoted: the song moved to ultimate because the JSON's ultimate is not in
    the catalogue, or None.
    """
    by_exact, by_lower = {}, {}
    for sid, name, rarity in songs:
        by_exact.setdefault(str(name), []).append((sid, name, rarity))
        by_lower.setdefault(str(name).lower(), []).append((sid, name, rarity))

    target, current, names = {}, {}, {}
    for sid, name, rarity in songs:
        current[sid], names[sid] = rarity, str(name)
    missing_in_db, first_found = [], None
    for name, new in rarities.items():          # most-streamed first
        rows = by_exact.get(name) or by_lower.get(name.lower())
        if not rows:
            missing_in_db.append(name)
            continue
        first_found = first_found or name
        for sid, _, _ in rows:
            target[sid] = new

    # Exactly one ultimate: if the JSON's is not in the catalogue, the
    # most-streamed song that is takes its place.
    promoted = None
    if target and "ultimate" not in target.values() and first_found:
        rows = by_exact.get(first_found) or by_lower.get(first_found.lower())
        promoted = (first_found, rarities[first_found])
        for sid, _, _ in rows:
            target[sid] = "ultimate"

    # Songs kworb does not list: basic, unless they already have a tier.
    unlisted = []
    for sid in current:
        if sid in target:
            continue
        old = current[sid]
        new = old if old in RARITY_ORDER else "basic"
        target[sid] = new
        unlisted.append((names[sid], old, new))

    changes, unchanged = [], 0
    for sid, new in target.items():
        if current[sid] == new:
            unchanged += 1
        else:
            changes.append((sid, names[sid], current[sid], new))
    return changes, unchanged, missing_in_db, sorted(unlisted), promoted


def write(changes):
    """One UPDATE per tier, all in one transaction."""
    by_tier = {}
    for sid, _, _, new in changes:
        by_tier.setdefault(new, []).append(sid)
    conn = db.get_connection()
    try:
        c = conn.cursor()
        db._begin(conn)
        updated = 0
        for tier, ids in by_tier.items():
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                c.execute(f"UPDATE songs SET rarity = ? WHERE id IN ({', '.join('?' * len(chunk))})",
                          (tier, *chunk))
                updated += max(c.rowcount, 0)
        conn.commit()
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Apply a rarity JSON to an artist's songs.")
    parser.add_argument("json", help="rarity file, e.g. rarities/niall_horan.json")
    parser.add_argument("--artist", help="catalogue artist name (default: from the file name)")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: preview)")
    args = parser.parse_args()

    rarities = load_rarities(args.json)
    artist = resolve_artist(args.json, args.artist)
    songs = artist_songs(artist)
    changes, unchanged, missing_in_db, unlisted, promoted = plan(rarities, songs)

    where = "Postgres" if db.IS_POSTGRES else f"SQLite ({db.DB_FILE})"
    print(f"{artist}: {len(songs)} songs in {where}, {len(rarities)} in {args.json}\n")

    order = {r: i for i, r in enumerate(RARITY_ORDER)}
    for sid, name, old, new in sorted(changes, key=lambda c: (order.get(c[3], 99), c[1].lower())):
        print(f"  {old or 'unassigned':<11} -> {new:<10} {name}")
    print(f"\n{len(changes)} to change, {unchanged} already right")

    final_by_id = {sid: r for sid, _, r in songs}
    for sid, _, _, new in changes:
        final_by_id[sid] = new
    final = Counter(final_by_id.values())
    print("after: " + "  ".join(f"{r}: {final.get(r, 0)}" for r in RARITY_ORDER)
          + (f"  unassigned: {final.get(db.UNASSIGNED, 0)}" if final.get(db.UNASSIGNED) else ""))

    if missing_in_db:
        print(f"\nIn the JSON but not in the catalogue ({len(missing_in_db)}) -- skipped:")
        for name in missing_in_db:
            print(f"  {name}")
    if promoted:
        print(f"\nThe JSON's ultimate is not in the catalogue; {promoted[0]!r} "
              f"(most streamed of those that are) is ultimate instead of {promoted[1]}.")
    if unlisted:
        print(f"\nIn the catalogue but not in the JSON ({len(unlisted)}) -- unassigned ones become basic:")
        for name, old, new in unlisted:
            note = "basic" if old != new else f"keeps {old}"
            print(f"  {name}   ({note})")

    if not changes:
        return
    if not args.apply:
        print("\nPreview only. Run again with --apply to write these changes.")
        return
    updated = write(changes)
    print(f"\nUpdated {updated} songs. The bot picks them up on the next pull "
          f"(cached catalogue views refresh within {db.CATALOG_CACHE_TTL // 60} minutes).")


if __name__ == "__main__":
    main()
