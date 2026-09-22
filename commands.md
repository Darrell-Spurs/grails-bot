# Grails-Bot — Command Reference

Every command a player can use: **25** in total. Admin commands are listed
separately at the bottom and are gated behind the **grails-admin** role.

**How to read the "Invoked as" column**

| Label | Meaning |
|---|---|
| **slash only** | Type `/name`. The `.name` form is deliberately ignored. |
| **prefix only** | Type `.name`. It never appears in the slash picker. |
| **hybrid** | Both `/name` and `.name` work. |

Angle brackets `<x>` mark a required argument, square brackets `[x]` an
optional one.

---

## 1 · Drops

The core loop. These are prefix-only because they get typed dozens of times in
a row, and `/` would slow that down.

| Command | Aliases | Invoked as | What it does |
|---|---|---|---|
| `.choice` | `.c` | prefix only | Three songs to pick from. Costs 1 drop charge |
| `.sigvinyl` | `.sv` | prefix only | Opens a signature vinyl — guaranteed signature art |
| `.vinyl` | `.v` | prefix only | Opens a vinyl. 10% chance it rolls up to signature odds |
| `/cooldown` | `.cooldown`, `.cd` | hybrid | Drop charges, with live countdowns to the next one and to full |

You bank one drop every **3 minutes**, up to **20**.

## 2 · Trading

| Command | Parameters | Invoked as | What it does |
|---|---|---|---|
| `/trade` | `<user> [rarity] [artist] [item]` | slash only | Offer one of your cards to another player |
| `/offer` | `[rarity] [artist] [item]` | slash only | Answer an open trade with a card of your own |
| `/canceltrade` | — | hybrid (`.ct`) | Withdraw your pending trade |
| `/gift` | `<user> [rarity] [artist] [item]` | slash only | Give a card away outright — one-way, nothing comes back |

`rarity` and `artist` are both filters, not requirements — they narrow the
`item` list, which matters once a collection gets large, and either on its own
is enough to fill it. With neither set the list waits until you have typed two
characters, and its results are labelled with their variant since they can span
all of them. Omit everything and you offer your newest card.

The two sides of a trade **do not have to match**. A mythic can be traded for a
default; each card keeps its own variant across the swap, keeping its row and so
its mythic copy number.

`/gift` asks before it moves anything: it posts a **Gift pending** card with
**Send** and **Cancel**, and only the gifter can press either. The card does not
change hands until Send, which matters because a gift has no counter-offer and
no undo.

A posted trade request carries an **Offer a card** button, so the other player
never has to type `/offer` at all. It opens a private dropdown of their newest
25 cards, plus **Search by name** for anything past that — a dropdown takes no
typed input, so the search opens a small text box instead.

`/canceltrade` is the exception: it takes no arguments, so it keeps a prefix
form and the short `.ct` alias. A trade request cancels itself after **2
minutes** if nobody answers it, with the same result as running the command.

## 3 · Your account

| Command | Parameters | Invoked as | What it does |
|---|---|---|---|
| `/register` | — | slash only | Create an account. Grants **1 signature vinyl** |
| `/collection` | `[variant] [user] [artist]` | slash only | Browse a collection — paged, filterable, sortable |
| `/profile` | `[user]` | slash only | Pinned card, favourite artist, totals, rarity spread |
| `/view` | `[item] [user]` | slash only | One card in full. Defaults to your latest pull |
| `/xp` | `[user]` | slash only | Level, progress bar, XP to the next level |
| `.daily` | — | hybrid | Claim the daily reward — 100–1000 XP, once per 24h |
| `.vinylcheck` | `[user]` | hybrid (`.vc`) | How many vinyl and signature vinyl you hold |

Registration is required before anything else — there is no automatic sign-up.

## 4 · Catalogue

| Command | Parameters | Invoked as | What it does |
|---|---|---|---|
| `/artists` | — | slash only | Every artist in the catalogue |
| `/albums` | `<artist>` | slash only | That artist's releases, grouped album / EP / single |
| `/songs` | `<artist> [rarity]` | slash only | That artist's songs, optionally one tier |
| `/mythiccheck` | `<artist> <song>` | slash only | Mythic copies claimed and remaining |

## 5 · Help

| Command | Invoked as | What it does |
|---|---|---|
| `/help` | slash only | Every command you can use |
| `/xphelp` | slash only | How XP, levels and vinyl rewards work |
| `/guide` | slash only | Link to the wiki |
| `/ping` | slash only | Bot latency |

## 6 · Games

| Command | Parameters | Invoked as | What it does |
|---|---|---|---|
| `/songbattle` | `[rounds]` | slash only | Multi-round song battle |
| `.battle` | — | prefix only (`.b`) | Quick single battle |
| `/leaderboard` | `[count]` | slash only | XP leaderboard, 1–25, with your own rank |

---

## Rarities and variants

A card is a **rarity** (how scarce the song is) crossed with a **variant** (how
it was obtained). Both show as a single server emoji.

**Rarity**, rarest first: `ultimate` · `legendary` · `elite` · `unique` ·
`basic`. A song that has been imported but not yet given a tier shows as
`unassigned` and cannot be pulled.

**Variant**, most desirable first: `mythic` · `sig_vinyl` · `vinyl` · `sketch` ·
`glitched` · `default`.

Mythics are capped server-wide — once every copy is claimed, no more exist.

---

## Admin commands

**20**, all prefix-only and gated behind the **grails-admin** role, so they
never reach the slash picker. Run `.adminhelp` in the server for the current
list with descriptions.

| Area | Commands |
|---|---|
| Catalogue | `.addartist` (`.aa`), `.removeartist` (`.ra`), `.list` |
| Rarity | `.rarity` (`.r`), `.showrarity` (`.sr`), `.assign` (`.a`) |
| Mythic | `.hunt`, `.checkhunt` (`.ckh`), `.clearhunt` (`.clh`), `.mythicstatus`, `.testmythic` |
| Grants | `.give_xp` (`.gxp`), `.givevinyl` (`.gv`), `.give_sigvinyl` (`.gsv`) |
| Moderation | `.list_latest` (`.ll`), `.remove_latest` (`.rl`), `.unregister` |
| Assets | `.emojicheck` (`.ec`), `.sigcheck` (`.sc`) |
| Help | `.adminhelp` |

---

## Notes for maintainers

New or renamed slash commands do not appear until an admin runs `.sync` in the
server. Prefix commands take effect as soon as the bot restarts.

A hybrid command's aliases are **not** registered as slash commands — `.cd`
works, `/cd` does not. For the same reason a slash-only command should never
declare aliases: they are unreachable by either route.
