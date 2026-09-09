# Grails-Bot — Implementation Report

This file has two parts:
- **[Part 1](#part-1--performance-security--database)** — first batch: performance, security, and database (with before/after benchmarks).
- **[Part 2](#part-2--gameplay-slash-commands--code-quality)** — second batch: Sour Patch Kids safeguard + bonus, slash commands + autocomplete, auto-register, help rework, and logging/dedup cleanup.

---

# Part 1 — Performance, Security & Database

This documents the changes made against the plan in `suggestion.md`, and reports measured
before/after performance. **Read the "Honest caveats" box** — a couple of the raw speedups are
partly environmental, and this report separates real wins from measurement noise.

Benchmarks were run against the live `music.db` (1,857 songs · 564 albums · 10,787 collections ·
6 users) with real Spotify network calls and real image generation. Harness:
`scratchpad/benchmark.py` (before) and `benchmark_after.py` (after).

---

## Answers to your two questions

**Q4 — Does `discord.File` have a count/rate limit when built from `io.BytesIO`?**
No count limit — you can create as many `discord.File(BytesIO(...))` objects as you like; nothing
is written to disk, so there are no filename collisions. The real limits are:
- **Upload size**: 25 MB per message for a normal bot (Discord's attachment cap). Our GIFs are now
  ~90–200 KB, so this is a non-issue.
- **API rate limits**: ~5 messages/channel/5s, handled automatically by discord.py's HTTP layer.
- **Gotcha**: a `File`/`BytesIO` is *single-use* — it's consumed when sent. Build a fresh one per
  send (we do). That's why in-memory beats shared files: no race, no cleanup, no disk I/O.

**Q6 — Ordered pull list vs. reroll loop: more convenient or more DB-heavy?**
I kept your existing pull *distribution* (random artist → weighted rarity → random song) because
game-mechanics changes are deferred to your later prompt. The dedup is now centralized in one
helper (`pick_unique_song`) and runs on the **now-indexed** `songs(artist, rarity)` +
`collections(song_id, variant)`, so the whole dedup flow dropped from **26.2 ms → 2.6 ms**. A
single `ORDER BY RANDOM() … WHERE NOT owned` query would be marginally fewer round-trips, but it
would change the odds to uniform-per-song — so I deliberately did **not** do that here. Revisit it
in the mechanics prompt.

---

## What changed, by plan item

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1 | Offload HTTP/img/DB to threads | ✅ | Heavy work (`get_random_song` block, image/GIF gen) wrapped in `asyncio.to_thread` in `choice_cog` and `xp_cog`. Trivial single-value DB calls left synchronous on purpose (see caveats). |
| 2 | Mythic = static cover + animated border | ✅ | `mystic.py`: cover + glow composed **once**; only the wave border redraws per frame. Frames 60→30. |
| 3 | Vinyl static, sig vinyl animated | ✅ | Normal `.vinyl` now sends a **static PNG**; the 10% sig branch and `.sigvinyl` stay animated GIFs. |
| 4 | BytesIO instead of shared files | ✅ | All pull media returns `BytesIO`; no more `choices.jpg` / `mythic_animated.gif` / `spinning_vinyl*.gif` race on disk. |
| 5 | Cache hot reads / faster queries | ✅ (partial) | Artist list cached (5-min TTL, invalidated on artist add). Single-query random pull **not** done (would change mechanics — deferred). |
| 6 | Dedup loop | ✅ | Centralized in `pick_unique_song`; benefits from new indexes. Distribution preserved. |
| 7 | Remove duplicate `get_filtered_albums`; cache Spotify token | ✅ | `get_songs_from_album` now returns the album list it already fetched (no second Spotify paging). Token cached ~1h. |
| 8 | Fresh-deploy `id` column | ✅ | `init_db()` now declares `users(id, username, xp, last_daily_claim, vinyl_count, sig_vinyl_count)` — matches the code. Idempotent migration adds any missing columns to old DBs. |
| 9 | `.env` + `.gitignore` | ✅ | Spotify creds moved to `.env`; DB + generated artifacts gitignored and `git rm --cached`'d. |
| 10 | Indexes + FK enforcement | ✅ | 6 new indexes; `PRAGMA foreign_keys=ON` per connection. See behavior note below. |
| 12 | Update report | ✅ | This file. |

Deferred by you (untouched): slash commands / auto-register / mechanics (item 11), the
`draw_variant()` `-10` mythic-forcing (intended for testing), `get_random_song` empty-result crash,
deployment model, `songs_cog.py` `.song` command (already broken before these changes — pre-existing).

---

## 📊 Before / After (mean wall-clock, ms)

```
metric                                          before     after   speedup
── Database (now indexed) ─────────────────────────────────────────────────
db.user_exists                                    3.08      0.31     9.8x   * cache warmth
db.get_user_xp                                    3.30      0.34     9.7x   * cache warmth
db.get_song_rarity                                3.26      0.31    10.6x   * cache warmth
db.get_all_artists                                3.35      0.35     9.6x   * cache warmth
db.get_songs_by_artist_and_rarity                 5.05      0.55     9.1x   ✔ index
db.get_album_details_by_song                      3.46      0.35     9.9x   * cache warmth
db.get_user_song_by_details                       6.01      0.81     7.4x   ✔ index
db.get_available_mythic_songs_for_user           11.67      3.34     3.5x   ✔ index
db.get_collection_with_copy_numbers              23.75     15.45     1.5x   ✔ index (subquery-bound)
db.get_collection_by_rarity_w_copies              7.61      1.18     6.5x   ✔ index
db.get_vinyl_count                                4.34      0.33    13.1x   * warmth + no per-call ALTER
── Pull flows ─────────────────────────────────────────────────────────────
get_random_song                                   8.63      0.69    12.4x   ✔ cached artist list
get_random_song + get_random_album               16.82      1.06    15.8x   ✔ cache + index
vinyl pull DB flow (dedup)                       26.18      2.56    10.2x   ✔ cache + index
── Network ────────────────────────────────────────────────────────────────
spotify.get_access_token                        141.47      0.00    ∞       ✔ token cache (steady state)
http.download_album_image                       311.44    129.31     2.4x   ✗ network variance only (unchanged code)
── Image / GIF generation (raw, includes 1–3 downloads) ───────────────────
make_3_song_collage (3 downloads + effects)    1532.96    869.97     1.8x   mostly network; render unchanged
vinyl static PNG                                410.82    238.34     1.7x   mostly network; render unchanged
vinyl GIF (500px/40f)                          3409.69   2403.36     1.4x   ✔ precomputed layers
sig vinyl GIF (500px/40f)                       3459.62   2480.47     1.4x   ✔ precomputed layers
mythic GIF (60f → 30f)                          2886.05   1302.74     2.2x   ✔ static glow + half the frames
```

### 🔦 Event-loop responsiveness — the headline result
Measured as the **max stall of a 5 ms heartbeat while a sig-vinyl GIF renders**:

```
BEFORE (sync, blocks the loop):   max heartbeat stall = 2654.4 ms
AFTER  (asyncio.to_thread):       max heartbeat stall =   32.8 ms
```

This is the core scalability win: previously a single pull froze **everyone's** commands, button
clicks, and the gateway heartbeat for ~2.7 s. Now the loop keeps ticking; the render happens on a
worker thread.

### 💥 Biggest practical per-command win
Normal `.vinyl` (the 90% common case) went from generating a **3.4 s spinning GIF** to a
**~0.24 s static PNG** — and that work is now off-loop. That's the switch from animated→static
(item 3), not a micro-optimization.

---

## ⚠️ Honest caveats (so the numbers aren't oversold)

1. **`* cache warmth`** rows: the flat ~10× on trivial primary-key lookups (`user_exists`,
   `get_song_rarity`, `get_vinyl_count`, …) is **mostly OS page-cache warmth** between the two runs
   (the before run hit a cold cache; init_db + smoke tests warmed it). Those queries hit a PK and
   **don't use the new indexes**, so their speedup is largely environmental, not my change. The
   genuine, attributable index wins are the JOIN/subquery-heavy queries marked `✔ index`.
   Removing the per-call `ALTER TABLE` did genuinely help `get_vinyl_count`.
2. **`http.download_album_image` (2.4×)**: I did **not** change this — it's pure network variance
   (311 ms vs 129 ms at different times). Because every generator downloads first, this variance
   inflates the *raw* image-gen speedups. Subtracting the download, the real CPU-side gen wins are
   more modest: mythic ≈ **2.2×** (30 frames + precomputed glow), vinyl/sig GIF ≈ **1.35×**
   (precomputed layers), collage ≈ ~1.2× (render essentially unchanged — I only made it return
   bytes).
3. The **token cache** `0.00 ms` is steady-state (cache hit). The **first** call after startup (or
   after ~1 h expiry) still costs ~140 ms — as designed.

---

## Behavior changes & things to verify

- **FK enforcement is now ON.** `remove_songs_by_artist` was updated to also delete the artist's
  rows from `collections` first (otherwise the parent delete now raises). Net effect: removing an
  artist now also removes users' collected copies of that artist (previously those were left
  orphaned). Verify this matches your intent for the admin remove flow.
- **Normal `.vinyl` is no longer a spinning record** — it's a static vinyl image. Sig vinyl and the
  10% sig upgrade inside `.vinyl` remain animated.
- **Mythic cover no longer pulses/animates** — only the border wave animates; the cover + glow are
  static.
- **Rotate the Spotify secret.** It was hardcoded and is in git history; assume compromised. New
  values live in `.env` (also add them to your Replit Secrets for deploy).
- **Git**: `music.db`, `choices.jpg`, `mythic_animated.gif`, `glitched_image.jpg`,
  `pencil_sketch_output.png` were `git rm --cached`'d (kept on disk, no longer tracked). Commit the
  `.gitignore` + these removals when you're ready.

## Not yet reproduced against a live Discord session
All modules import, compile, and generate valid `BytesIO` output (verified), and the benchmarks
exercise the real generation/DB paths. I could **not** drive an actual `.vinyl`/`.choice` through a
live gateway connection from here, so please smoke-test one of each pull in a test channel.

---

# Part 2 — Gameplay, Slash Commands & Code Quality

Second batch of work. The random-pull odds rewrite and the deployment-model change were **left
alone on purpose** (deferred), and `.choice`'s `draw_variant` mythic-forcing was already reverted by
you. `requirements.txt` was fixed manually.

## What changed, by item

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1 | Hold random-pull odds rewrite + deployment | ⏸️ deferred | Untouched, as requested. |
| 2 | `.choice` test ended (`random_num = -10` reverted) | ✅ (by you) | Verified the override is commented out. |
| 3 | Safeguard `get_random_song()` crash → Sour Patch Kids | ✅ | See below. |
| 4 | Slash commands + song/artist autocomplete | ✅ (core) | Hybrid (prefix **and** slash) for the core user commands. |
| 5 | Auto-register on interaction | ✅ | `bot.before_invoke` registers any user on their first command (prefix or slash). |
| 6 | Rework `.help` (real commands, user vs `.help admin`) | ✅ | Generated from actually-registered commands. |
| 7 | Logging, dedup vinyl, avoid bare `except` | ✅ | In the touched files. |
| 8 | `requirements.txt` | ✅ (by you) | — |
| 9 | More tests/updates | ⏸️ later | — |
| 10 | Sour Patch Kids bonus after `.choice` | ✅ | 1% chance, separate message, 50% level-up / 50% vinyl. |
| 11 | This report | ✅ | Part 1 above, Part 2 here. |

## #3 — `get_random_song()` crash safeguard
- `get_random_song` now **rerolls** (artist, rarity) up to 25× until it finds a real song, so a random
  pair with no songs no longer crashes on `random_song[None]`.
- If nothing is found at all (essentially only an empty catalog), it returns a **Sour Patch Kids**
  sentinel (`__sourpatchkids__`) whose image is [utils/images/sourpatchkids.png](utils/images/sourpatchkids.png).
- `get_random_album` returns that local image for the sentinel; `make_3_song_collage` now renders
  **local files** (not just URLs) and tolerates a `None` rarity.
- The sentinel is **non-collectible**: `.choice` selection replies "not collectible"; `.vinyl`/`.sigvinyl`
  **refund** the pull and show the Sour Patch Kids image. (This avoids a foreign-key error, since the
  sentinel has no real song/album row.)
- Verified end-to-end by forcing the empty-result path.

## #10 — Sour Patch Kids bonus event
After any `.choice`/`.c` result (the normal result still shows), there's a **1% chance** a separate
"wild Sour Patch Kids appeared!" message fires and gives either (50/50):
- **Level up to the next level** (adds the exact XP gap, then runs the normal level-up rewards), or
- **A free vinyl pull** (`+1` vinyl).

Wired into all three `.choice` completion paths (normal, mythic, gutscookie).

## #4 / #5 — Slash commands, autocomplete, auto-register
- **Hybrid conversion** (one definition works as **both** `.prefix` and `/slash`) for the core user
  commands: `choice`, `vinyl`, `sigvinyl`, `collection`, `xp`, `daily`, `vinylcheck`, `register`,
  `help`, `ping`. Verified: all 17 cogs load, all 10 appear in both the prefix set and the slash tree.
  *(`trade`/`offer`/`canceltrade` added in the #12 follow-up below.)*
- **Autocomplete** on `/collection`: rarity (the 6 rarities) and artist (from the catalog). `.collection`
  keeps its old free-text convenience (artist-only still works).
- **Auto-register**: `bot.before_invoke` lazily registers any user on their first command, so new users
  never hit the "not registered" wall (works for prefix and hybrid-slash).
- **`.sync` admin command** added (bot_admin only): run it in your server to register the slash commands
  instantly. **You must run this** — Discord doesn't show slash commands until the tree is synced.

## #12 — `/trade` slash command (follow-up)
Requested after the rest of Part 2 landed. Converted `trade`/`offer`/`canceltrade` to hybrid
(`.prefix` + `/slash`), and replaced the old free-text song matching with the same
autocomplete-first approach used for `/collection`:
- **New DB helpers**: `get_user_tradeable_items(user_id, rarity)` (indexed on
  `collections(user_id, variant)`) and `get_collection_item_by_id(collection_id, user_id)`
  (primary-key lookup, scoped to the caller so you can't reference someone else's item).
- **`/trade` and `/offer`** now take a `rarity` param (autocomplete: the 6 rarities) and an `item`
  param (autocomplete: the *caller's own* matching collection rows, as `"Song - Artist (Album)"`).
  Selecting a suggestion sends the item's `collections.id`, so resolution is an exact primary-key
  lookup — no guessing.
- **`.prefix` fallback preserved**: `_find_trade_item()` accepts either a numeric id (what
  autocomplete sends) or free text. Free text now does **one indexed query + substring filter**
  instead of the old O(n²) loop that tried every split point of the message against the DB
  ([cogs/trade_cog.py](cogs/trade_cog.py) previously ~lines 102-129) — this was flagged as a bug in
  the original review (§4 UX) and is now fixed as a side effect. Ambiguous matches list up to 10
  candidates instead of silently guessing wrong.
- The rest of the trade flow (2-minute request timeout, ✅/❌ reaction confirmation, 60s
  confirmation timeout, the actual collection swap) is unchanged.
- Replaced the `print(...)` debug lines in the old matching loop with nothing (the loop itself is
  gone) and added a `logging` logger for the trade-completion event.

**Follow-up: artist filter (requested after initial testing).** A player with many songs of one
rarity blew past Discord's **25-choice autocomplete cap** — e.g. one test account has 337 `default`
items but only 15 distinct artists. Added an optional `artist` parameter to `/trade` and `/offer`,
positioned between `rarity` and `item`:
- New autocomplete `_own_artist_autocomplete`: the caller's distinct artists for the chosen rarity.
- `get_user_tradeable_items(user_id, rarity, artist=None)` gained an optional exact-match artist
  filter (`songs.artist = ? COLLATE NOCASE`); `_own_item_autocomplete` now reads the already-typed
  `artist` from `interaction.namespace` to scope the item list before capping at 25.
- **Prefix-mode ambiguity handled explicitly**: because `artist` sits before the "consume rest"
  `item` parameter, prefix invocation (`.trade @user mythic Some Song Title`) will parse the first
  word into `artist` whether or not the user meant it as a filter. `_resolve_trade_item()` detects
  this (the mis-parsed "artist" matches zero real artists) and **retries treating the whole phrase
  as `item` with no artist filter** — recovering the original free-text behavior transparently.
  Verified directly: a real multi-word song title was fed through the exact split discord.py would
  produce, and the retry recovered the correct song.

Verified: slash tree shows `artist` as optional / `item` as required for both commands; all 17 cogs
still load; `_find_trade_item` and `get_user_tradeable_items` tested against the live DB (artist
filter narrows 337→32 correctly, a bogus artist zeroes results, id-lookup + ownership enforcement,
ambiguous-match handling); both autocomplete callbacks tested directly (artist list, and item list
correctly scoped to the chosen artist).

## #6 — `.help` rework
- `.help` / `/help` → **user** commands; `.help admin` / `/help admin` → **admin** commands.
- Generated from the **actually-registered** commands (names, aliases, one-line descriptions), so it can't
  drift from reality. Admin detection is dynamic (commands gated by `has_role`/`has_permissions`/`is_owner`).
- The stale hardcoded dict is gone. The default help command is disabled (`help_command=None`) so the
  custom one owns `help`.

## #7 — Code quality
- **Logging**: `print(...)` debug replaced with a module `logging` logger in the touched files
  (`bot.py`, `helpers.py`, `choice_cog.py`, `xp_cog.py`, `collection_cog.py`). *(Prints in the untouched
  admin/Spotify cogs remain — a follow-up.)*
- **Dedup**: `.vinyl` and `.sigvinyl` (~90% duplicated) now share one `_do_vinyl_pull(ctx, guaranteed_sig)`
  helper. (The `vinyl_gif_*` near-duplicates were already merged in Part 1.)
- **Bare `except:`** → specific `except discord.HTTPException:` for the DM-send and `fetch_user` paths.

## Behavior notes & things to verify (Part 2)
- **Run `.sync` once** in your server (as bot_admin) or slash commands won't appear.
- `.collection` prefix parsing changed slightly: the **first** token is treated as a rarity only if it's a
  real rarity; otherwise the whole thing is the artist (so `.collection Taylor Swift` still works).
- The Sour Patch Kids fallback (#3) is essentially unreachable with your populated DB — it only triggers
  on an (almost) empty catalog. It's verified via a forced test, not natural data.
- Auto-register runs a tiny `user_exists` check before every command; cheap, but it is one extra query
  per command.
- Not verified against a live gateway: slash registration/sync, autocomplete UX, and the reaction-based
  collection menu under `/collection`. Please smoke-test `/choice`, `/vinyl`, `/collection`, `/trade`
  + `/offer` (including the artist-filtered autocomplete and the ✅/❌ confirmation reactions), and
  `.help` / `.help admin` after syncing.
- **Re-run `.sync`** — `/trade`'s new `artist` option (and the command itself, if you haven't synced
  since it was added) won't show up until you do.
