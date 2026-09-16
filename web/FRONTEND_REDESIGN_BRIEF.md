# Grails-Bot Admin Frontend Redesign Brief

## Purpose

Use this document as the implementation brief for the next frontend redesign pass.

Grails-Bot Admin manages the catalog and player collections for a song-collecting game. It is an internal admin product, not a player-facing experience. It should feel clean, intuitive, informative, efficient, and aesthetically considered, but it should not resemble a marketing site or a highly decorative music application.

The backend behavior and existing data model should remain intact unless a UI requirement makes a backend change unavoidable.

## Approved direction

The selected album and song management pattern is **Design 3: Discography Workbench**.

This decision replaces the album accordion as the preferred desktop interaction:

- Left pane: persistent compact album list.
- Right pane: tracks for the selected album.
- Selecting an album changes the right pane without navigating away or closing the surrounding artist context.
- The administrator can move quickly between albums while keeping the editing surface in a stable position.
- On narrow screens, transform the left pane into a horizontally scrollable album selector or a list-to-detail flow. Do not squeeze both desktop columns into an unusable mobile split view.

The original accordion can remain only as a graceful narrow-screen fallback if that is substantially simpler and preserves functionality.

## Visual thesis

Use a **quiet record-library operations** aesthetic:

- Cool neutral page background and white or dark-mode surfaces.
- Restrained indigo accent color.
- Album artwork provides most of the expressive color.
- Compact, practical density appropriate for repeated admin work.
- Subtle borders and limited elevation rather than large shadows.
- A thin album-specific color strip may act like a record spine in album lists.
- Avoid oversized empty cards, decorative gradients, marketing-style statistics, and unnecessary animation.

Preserve the existing light/dark theme behavior and the current rarity and collection-variant color meanings.

## Typography — approved and already applied

Use **IBM Plex Sans** as the primary interface typeface and **IBM Plex Mono** for operational data.

### Roles

- IBM Plex Sans: navigation, page headings, artist and album names, body text, buttons, forms, tables, badges, and dialogs.
- IBM Plex Mono: timestamps, copy numbers, IDs, compact status values, and other data that benefits from fixed-width alignment.
- Use `font-variant-numeric: tabular-nums` for aligned counts and time values.

### Suggested scale

- Page heading: 30–34px, weight 600 or 650.
- Section heading: 17–19px, weight 600.
- Body and table cells: 14–16px, weight 400.
- Buttons and inputs: 14px, weight 500 or 600.
- Secondary metadata: 12–13px.
- Key statistics: 26–30px, weight 600 or 650.

Do not use very small uppercase table labels. Text used regularly should normally be at least 14px; reserve 12–13px for secondary metadata.

The current implementation loads IBM Plex Sans and IBM Plex Mono in `templates/base.html`. Global font ownership is defined through `--font-ui` and `--font-data` in `static/style.css`. Preserve this single shared ownership path instead of redefining fonts on individual screens.

## Iconography

Replace emojis and letter-based representations with one consistent SVG icon family. **Lucide** is preferred because its visual weight matches the current interface and icons can be rendered through shared Jinja macros.

Recommended mappings:

| Element | Icon |
| --- | --- |
| Songs | `Music2` |
| Albums | `Disc3` |
| Users | `UsersRound` |
| Search | `Search` |
| Mythic target | `Gem` |
| Theme | `Moon` / `Sun` |
| Mobile navigation | `Menu` |
| Copy status | `Copy` |
| Change target | `Pencil` or `Crosshair` |
| Expand or navigate | `ChevronRight` |
| Delete | `Trash2` |
| Missing artwork | `ImageOff` |

Render icons locally or through reusable inline SVG macros. Do not hotlink individual SVG files. Every icon-only control must have an accessible name. Do not replace every label with an icon; icons should support recognition, not force memorization.

Rarity and collection-variant badges should normally use a colored dot plus text instead of a different emoji for every value. Never rely on color alone.

## Dashboard

### Page heading

- Keep the `Dashboard` heading.
- Remove the subtitle “Catalog size, the active mythic hunt, and the newest pulls.”
- Avoid other text that merely repeats the section heading.

### Statistics

- Retain Songs, Albums, and Users.
- Replace their emojis with Lucide icons.
- Reduce card height slightly.
- Keep the values prominent and labels easy to scan.
- Because the cards navigate elsewhere, make navigation clearer with a subtle chevron or arrow-on-hover treatment.
- Do not add invented trends or percentages unless real data supports them.

### Current Mythic Hunt

- Reduce the artwork to approximately 96–112px.
- Present song, artist, album, and target status in one compact information block.
- Place Copy Status and Change Target actions at the trailing edge on desktop.
- Remove excessive unused horizontal space.
- Preserve artwork aspect ratio and do not overlay controls or badges on top of it.

### Latest Pulls

- Keep the table; it is an appropriate pattern.
- Remove the explanatory subtitle such as “Newest 0 collected songs across all players.”
- Keep song artwork, song/artist, variant, album, and collection time.
- Replace variant emojis with shared badge styling using text and a semantic color marker.
- Provide a useful, actionable empty state when no pulls exist.

## Artist catalog

### Artist representation

Remove first-letter avatars.

Preferred replacement: create a 2×2 mosaic from up to four album covers belonging to the artist.

- One album: show that album cover.
- Two or three albums: use a stable mosaic layout without stretching.
- No artwork: show a neutral shared `Music2` or `ImageOff` fallback.
- Show artist name, album count, and song count.

This avoids requiring a new artist-image field while making cards identifiable and music-specific.

### Search

- Keep a visible label or accessible name.
- Add an integrated clear button when the query is non-empty.
- Clearing must be immediate and return focus to the search input.
- Preserve committed search state in the URL if the existing route supports it.
- Distinguish an empty catalog from a search with no results.

### Spotify import

- Replace the oversized sparse import card with a compact toolbar or focused import panel.
- Keep enough copy to make the import scope clear, but remove redundant explanation.
- If import is asynchronous, provide stable pending, success, and recoverable error states.

## Artist detail

### Header

- Show artist name as the primary heading.
- Display counts compactly as inline metadata, for example `10 songs · 3 albums`, rather than as a separate sentence-style subtitle.
- Use an album-art mosaic instead of a letter avatar if an artist visual is needed.

### Rarity breakdown

Replace the segmented horizontal bar with a **donut-style pie chart**.

- Recommended diameter: 180–220px on desktop.
- Show total songs in the center.
- Place a legend beside it with rarity name, count, and percentage.
- Stack the legend below the chart on narrow screens.
- Use the same rarity colors as badges and selectors.
- Include text labels or an accessible summary so meaning is not color-only.
- Handle zero totals without division errors or a misleading full ring.

### Discography Workbench

#### Left album pane

- Album cover: approximately 48–56px.
- Show album name, release type, year when available, and track count.
- Use a clear selected state that remains distinguishable without color alone.
- Preserve the selected album when a rarity update completes.
- The pane may scroll independently on desktop, but its scrollbar must remain visible and usable.

#### Right track pane

- Use a semantic table for the track list.
- Header shows selected album artwork, album name, type, year, and track count.
- Suggested columns: track number, song, rarity, and save status.
- Keep the album header visible while scrolling a long track list when practical.
- Make long song names available rather than permanently hiding critical text behind truncation.

#### Rarity editing

- Immediate saving is acceptable for this internal tool.
- Show row-level states: `Saving…`, `Saved`, and a persistent recoverable error.
- Prevent duplicate requests for the same control while saving.
- Preserve selection, scroll position, and focus after success or failure.
- If the native `<select>` remains, accept that its opened popup is operating-system styled. Do not visually promise a fully custom popup while retaining a native control.

## Destructive actions

Replace `window.confirm()` with an application-owned accessible confirmation dialog.

- Name the artist and exact removal scope.
- State whether recovery is possible.
- Use `Cancel` and an explicit destructive verb such as `Permanently remove artist`.
- Initially focus Cancel for an irreversible operation.
- Keep the dialog open while the request is pending.
- Show errors inside the dialog with a retry path.
- Restore focus to a logical element after cancellation or completion.

If the backend can support archive or soft deletion, prefer that over irreversible removal. Do not advertise Undo unless the operation is genuinely reversible.

## Content and interface writing

- Use sentence case.
- Remove subtitles that repeat a heading or describe information already visible directly below.
- Keep copy that explains consequences, scope, errors, or recovery.
- Use consistent action labels across screens.
- Buttons should state the action: `Import artist`, `Change target`, `Save changes`, or `Permanently remove artist`; avoid `Submit` and `OK`.

## Responsive behavior

- Desktop: two-pane Discography Workbench.
- Tablet: narrower album pane with stable track table overflow.
- Mobile: horizontal album selector, list-to-detail transition, or accordion fallback.
- Do not create nested full-page scroll areas that trap the user.
- Ensure tables have an intentional narrow-screen strategy rather than silently clipping columns.
- Keep touch targets usable and keyboard focus visible.
- Respect `prefers-reduced-motion`.

## Accessibility and stability

- Target WCAG 2.2 AA.
- Use real buttons for actions and anchors for navigation.
- Every clickable element needs hover, focus, active, and disabled or busy styling when applicable.
- Reserve album-art dimensions to prevent layout shift.
- Do not move controls when loading or saving.
- Give icon-only buttons accessible names.
- Use semantic tables for read-oriented track data.
- Ensure the donut chart and colored rarity markers have equivalent text.
- Preserve keyboard behavior and focus when selecting albums and editing rarity.

## Implementation boundaries

- Preserve Flask routes, Jinja structure, authentication, database behavior, and existing domain rules unless a documented UX requirement needs a coordinated change.
- Prefer shared Jinja macros and shared CSS tokens over screen-local duplication.
- Use realistic fake data only for visual development and testing; do not ship it as production data.
- Do not add speculative analytics, trends, or administration features.
- Keep dark mode visually equivalent and ensure IBM Plex loads without a flash that changes layout significantly.

## Suggested implementation order

1. Keep IBM Plex as the global font system.
2. Establish a shared inline SVG icon macro and remove emoji controls.
3. Implement the artist album-art mosaic component.
4. Replace the rarity bar with the accessible donut chart and legend.
5. Build the responsive Discography Workbench on the artist-detail screen.
6. Compact the dashboard statistics and Mythic Hunt section.
7. Simplify subtitles, catalog search, and Spotify import presentation.
8. Replace the browser confirmation with the accessible destructive dialog.
9. Verify light/dark themes, keyboard operation, narrow layouts, long names, empty data, saving, failure, and reduced motion.

## Acceptance checklist

- [ ] Design 3 is the artist discography interaction on desktop.
- [ ] IBM Plex Sans and IBM Plex Mono are applied globally through shared tokens.
- [ ] No functional emoji icons remain in navigation, statistics, badges, search, or empty states.
- [ ] Artist letter avatars are replaced by album-art mosaics with a durable fallback.
- [ ] Rarity Breakdown uses an accessible donut-style pie chart.
- [ ] Redundant dashboard and section subtitles are removed.
- [ ] Latest Pulls remains a compact, useful table.
- [ ] Rarity autosave has visible saving, success, and error feedback.
- [ ] Selected album, focus, and scroll position survive rarity updates.
- [ ] Destructive removal uses an application-owned accessible dialog.
- [ ] Desktop and narrow-screen workflows remain complete.
- [ ] Light mode, dark mode, keyboard navigation, long content, empty states, and failure states are verified.

## External design references

- Lucide icons: <https://lucide.dev/>
- Material Symbols alternative: <https://fonts.google.com/icons>
- Spotify design guidance for artwork and metadata: <https://developer.spotify.com/documentation/design>
- IBM Plex: <https://github.com/IBM/plex>
