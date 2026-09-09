# Grails-Bot — Remaining Suggestions

The performance, security, and database items from the original review are **done** — see
[`suggestion_implementation.md`](suggestion_implementation.md) for what changed and the before/after
benchmarks. This file now tracks only the **outstanding** work (mostly UX, mechanics, and code
quality, plus a few things intentionally deferred).

---

## 1. Performance (remaining)

- **Weighted single-query random pull.** The random pull still does N Python-side steps (pick
  artist → pick rarity → query → reroll on dup). It could be one weighted SQL query
  (`ORDER BY RANDOM() LIMIT 1` with a rarity filter, excluding owned songs). **Not done on purpose:**
  it changes the drop distribution (uniform-per-song vs. the current artist-first). Revisit with the
  mechanics work (§3, drop-distribution intent).
- **Deployment model.** `.replit` uses `deploymentTarget = "cloudrun"` ([.replit:16](.replit#L16)).
  Cloud Run is request-driven and scales to zero / can spin up **multiple** instances — neither is
  right for a long-running gateway bot (scaled-to-zero disconnects the bot; two instances
  double-connect and both write the same `music.db`). Use a **Reserved VM / always-on single
  instance**. This also matters for the integrated web admin panel, which assumes one process.

---

## 2. Bugs intentionally deferred

- **`.choice` always rolls mythic.** [utils/helpers.py](utils/helpers.py) `draw_variant()` forces
  `random_num = -10`, so every `.choice` takes the mythic path. Currently kept **for testing** —
  remove the override (and the `print`) before opening the game up.

---

## 3. Database design (remaining)

- **Rarity isn't snapshotted into the collection.** `collections` stores `variant` but not `rarity`;
  rarity lives only on `songs.rarity`. So an admin re-assigning a song's rarity retroactively changes
  the rarity of every copy already owned — and XP was granted based on the rarity *at pull time*.
  Snapshot `rarity` into `collections` at collect time (as you already do for `variant`).
- **Broken/stub helper.** `remove_mythic_from_collection()` takes no args and can't target a row
  ([db.py](db.py)) — looks unfinished.

---

## 4. UX & onboarding (making it easier to get in)

- **Switch remaining commands to slash.** Core user commands (`choice`, `vinyl`, `sigvinyl`,
  `collection`, `xp`, `daily`, `vinylcheck`, `register`, `help`, `ping`) and now `trade`/`offer`/
  `canceltrade` are hybrid (`.prefix` + `/slash`) with autocomplete. Still prefix-only: the
  `bot_admin` commands (`hunt`, `clearhunt`, `checkhunt`, `mythicstatus`, `givevinyl`,
  `give_sigvinyl`, `assign`, `rarity`, `addartist`, `removeartist`, `save`, `list`, `album`,
  `single`) and `unregister`. Prefix (`.`) commands require the privileged **message-content
  intent**, which Discord gates at scale (verification at 100+ servers) — migrating the rest closes
  that gap.
- **Inconsistent feedback.** Some failures are silent (e.g. `assign` swallows a `ValueError` and does nothing — [cogs/assign_cog.py:71-78](cogs/assign_cog.py#L71-L78)); others are verbose. Standardize an embed style and always confirm success/failure.
- **Onboarding flow.** A short `.start`/welcome (how pulls, XP, vinyl, rarities, trading work) plus a first-pull "tutorial" would convert new joiners. The `rewards`/`level` info exists but is scattered.

---

## 5. Game-mechanic suggestions

- **Fix drop distribution intent.** `get_random_song` picks a **uniform random artist**, then a rarity, then a song ([utils/helpers.py](utils/helpers.py)). So an artist with 3 songs is as likely as one with 50, and per-song odds are uneven. Decide what you *want* (uniform per song? weighted by popularity/rarity?) and implement it directly in SQL. (This is the same call as the single-query pull in §1.)
- **Persist progression rewards.** `claimreward` records claims in an **in-memory dict** ([cogs/xp_cog.py:17](cogs/xp_cog.py#L17)) — wiped on restart, so users can re-claim or lose claims. Same for `user_levels`. Persist to the DB.
- **Trades are ephemeral.** Trade state is in-memory ([cogs/trade_cog.py:82](cogs/trade_cog.py#L82)); a restart mid-trade loses it, and there's no history/audit. (Item selection itself was fixed — see §4/§6 — but the trade *session* still isn't persisted.) Persist trades (pending + completed) — enables a trade log, dispute handling, and mod oversight (ties into the web admin panel).
- **Economy depth.** The 3-copy mythic scarcity is a great hook. Consider building on it: duplicate → "dust"/crafting currency, set-completion bonuses (collect a full album), leaderboards (rarest collection, most complete), timed events, daily streak multipliers, and a **pity timer** for mythic so unlucky users still progress.
- **`level` command is dead.** It's gated behind `@commands.has_role("no_one")` ([cogs/xp_cog.py:198](cogs/xp_cog.py#L198)) so nobody can run it. Either restore it or delete it.

---

## 6. Code quality & maintainability

- **Use `logging`, not `print`.** *(Done in `bot.py`, `helpers.py`, `choice_cog.py`, `xp_cog.py`,
  `collection_cog.py`, `trade_cog.py`.)* Still full of `print(...)` debug in `addartist_cog.py`,
  `album_cog.py`, `assign_cog.py`, `list_cog.py`, `single_cog.py` — convert the rest as they're touched.
- **De-duplicate.** *(`.vinyl`/`.sigvinyl` and the `vinyl_gif_*` near-duplicates were already consolidated; `trade`/`offer`'s shared item-lookup is now the single `_find_trade_item` helper.)* Keep an eye out for new duplication as remaining admin cogs get touched.
- **Avoid bare `except:`.** *(Fixed in `xp_cog.py`.)* One remains: [utils/helpers.py:450](utils/helpers.py#L450)
  (the rarity-font fallback in `make_3_song_collage`) swallows everything — narrow it to the font-load
  exception.
- **`requirements.txt` hygiene.** Duplicates (`Pillow` and `watchdog` listed twice — [requirements.txt](requirements.txt)) and mostly unpinned versions. Pin versions and de-dup for reproducible deploys.
- **Remove stray/dead code.** `grails/bot.py` is an unused duplicate of the entrypoint; `test.py`, the broken `cogs/songs_cog.py` `.song` command, and commented-out blocks add noise. Prune them.
- **No tests.** Even a handful of unit tests around `db.py` (XP math, mythic copy limits, rarity queries) and `draw_variant`/`get_random_song` would catch regressions like §2. Add `pytest` against a temp SQLite file.

---

## Suggested roadmap (remaining)

**Before launch:** remove the `draw_variant` mythic override (§2) · guard `get_random_song` (§2).

**UX push:** migrate the remaining admin commands to slash (§4) · standardize feedback embeds ·
add a first-pull onboarding flow.

**Mechanics + persistence:** decide drop distribution and move it into SQL (§1/§5) · persist
progression + trade state to the DB (§5) · snapshot rarity into `collections` (§3).

**Later (structural):** fix the deployment model to always-on single instance (§1) · plan the
SQLite→Postgres path if you outgrow it · add a test suite (§6).
