---
name: Vinyl Now Playing
description: A companion display for vinyl playback — album art, track metadata, and per-cover theming.
colors:
  bg: "#0a0a0a"
  surface: "#161616"
  accent: "#c8c8c8"
  text: "#ebe6dc"
  muted: "#8a857c"
  canvas-chrome: "#f0eee9"
typography:
  display:
    fontFamily: "Inter Tight, DejaVu Sans, Arial, sans-serif"
    fontSize: "72px"
    fontWeight: 600
    lineHeight: 1.0
    letterSpacing: "-0.03em"
  headline:
    fontFamily: "Inter Tight, DejaVu Sans, Arial, sans-serif"
    fontSize: "48px"
    fontWeight: 500
    lineHeight: 1.04
    letterSpacing: "-0.022em"
  title:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "32px"
    fontWeight: 400
    lineHeight: 1.12
    letterSpacing: "normal"
  catalog:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "0.08em"
  label:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "0.16em"
rounded:
  none: "0px"
  chip: "2px"
spacing:
  strip-padding: "26px"
  grid-gap: "44px"
  artboard-inset: "50px"
components:
  genre-chip:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.chip}"
    padding: "5px 12px"
---

# Design System: Vinyl Now Playing

## 1. Overview

**Creative North Star: "The Listening Room Monitor"**

A screen that earns its place next to a turntable and good speakers. It sits quietly in the room while the record plays — noticeable when glanced at, invisible when not. The design doesn't perform. It informs.

Each record defines its own visual world. The palette is extracted from the cover and applied to every surface: background, text, accent, depth. The result is that Sonic Youth and Aimee Mann feel genuinely different — not as a feature, but as a consequence of treating each record as its own artifact. The display adapts; it does not assert.

Typography carries the same logic. The track name is large and tight because it is the thing you want to know. The artist is large because you want to confirm it. The album title is set in a warm italic serif because it is the one element that requires warmth. Everything else is JetBrains Mono — catalog numbers, side positions, status indicators — because those are data points, not copy.

This system explicitly rejects the streaming-app aesthetic (Spotify, Apple Music): no rounded album tiles, no soft gradient scrubbers, no "Now Playing" headers with shuffle icons. The display is not a player. It has no controls and no chrome that implies it might. It is also not a timeline — no progress bars, no elapsed time, no duration. Vinyl is not an experience you track; it is one you inhabit.

The target device is the Waveshare 7" HDMI LCD (H) at exactly 1024×600, driven by a Raspberry Pi. This is a fixed-size deployment, not a responsive one. The artboard dimensions are the screen dimensions.

**Key Characteristics:**
- Per-album palette theming: every record tints its own display
- Sharp corners throughout; no decorative rounding
- Monospaced labels for all technical/metadata text
- Deep ambient shadow under cover art; tonal layering for surface depth
- Compressed display typography; generous breathing room between elements
- Fallback palette (below) activates when no album is loaded
- Smooth 1-second lerp between palettes on track change — no jump cuts

**Implementation note:** `Vinyl Now Playing.html` (React/Babel) is the primary design prototype; all geometry in this document is derived from it. The production renderer is Python/Pillow/pygame (`src/display/layouts.py`, `src/display/renderer.py`). The two are intentionally kept in sync — the prototype is the design-intent source.

## 2. Colors: The Adaptive Palette

The color system is semantic, not fixed. Five roles; each album fills them from its cover. The values below are the fallback — active on boot, idle, and "no record" states.

### Primary
- **Deep Void** (`#0a0a0a`): The main background. Near-black, achromatic. Each album replaces this with its own dark tint extracted from the cover.
- **Surface Lift** (`#161616`): The second layer — slightly lighter than bg, used as the radial gradient endpoint to create depth without a visible edge. Each album replaces this with its own surface tone.

### Secondary
- **Extract Accent** (`#c8c8c8`): The accent role: track name glow color, status dot color, genre chip border. Every album fills this from the brightest extracted hue in the cover. The fallback is light silver.

### Neutral
- **Near-White Text** (`#ebe6dc`): Primary text. Warm off-white; slight warmth prevents harshness. Each album replaces this with a tinted near-white matching the cover's dominant hue.
- **Muted Secondary** (`#8a857c`): Secondary text. Status labels, catalog text, PREV/NEXT labels, genre chip text. Must pass 4.5:1 contrast against its album bg at full opacity — never stack with additional opacity reductions. When extracting a muted value for a new album, verify the ratio; cool-dark backgrounds (e.g., Cavetown `#0e1a2a`) pull contrast down faster than neutral darks.

**Canvas Chrome** (`#f0eee9`): The design-tool background. Never part of the now-playing display.

### Named Rules

**Palette transitions** are smooth: all five roles lerp simultaneously over ~1 second when a track change triggers a new palette. This is a production feature, not a design enhancement — design work should treat the transition as a given and not work around it.

**The Full-Opacity Rule.** Secondary text (`muted`) already conveys its subdued role through hue. Never compound with opacity. `p.muted` at `opacity: 0.65` over a dark bg fails contrast. Use the color as-is; reduce saturation or lightness in the palette if it reads too heavy.

**The Per-Album Rule.** The five palette roles (`bg`, `surface`, `accent`, `text`, `muted`) are architecture, not values. Treat the fallback palette as the null state; treat each album's extraction as the real design. New album additions require a new palette entry in `data.js`, not a design change.

**The Hue Diversity Rule.** Extracted `accent` colors must maintain ≥60° hue separation (in OKLCH/LCH space) from every other album's accent in the collection. Unconstrained extraction clusters toward warm orange (hue 40–60°), making distinct records indistinguishable at a glance. When a new extraction lands within 60° of an existing accent, either: (a) use the cover's second-most-prominent hue, or (b) shift the accent's hue ±60°+ while preserving its lightness and chroma. The five prototype albums span the hue wheel: orange (Sister ~55°), olive (Bachelor ~125°), blue (Repeater ~225°), violet (Bush of Ghosts ~290°), rose (Cavetown ~345°). All pairs maintain ≥60° separation.

## 3. Typography

**Display Font:** Inter Tight (with DejaVu Sans, Arial, sans-serif fallback)
**Title Font:** Newsreader, italic (with Georgia, serif fallback)
**Label / Mono Font:** JetBrains Mono (with ui-monospace, monospace fallback)

**Character:** A tight geometric sans paired with a warm editorial serif, mediated by a precise monospace. The sans compresses; the serif breathes; the mono anchors. Three families with maximum contrast between them — structural, warm, technical.

### Hierarchy
- **Display** (Inter Tight 600, 72px, lh 0.98, ls -0.03em): Track name. The single most important piece of information. `textWrap: balance` to prevent awkward orphans. Set in `p.text`.
- **Headline** (Inter Tight 500, 48px, lh 1.04, ls -0.022em): Artist name. Large but lighter weight than the track name to establish clear subordination. Set in `p.text`.
- **Title** (Newsreader italic 400, 32px, lh 1.12): Album title only. The one serif-italic moment. Set in `p.accent` — the color contrast ties the album name to the cover.
- **Chip** (JetBrains Mono 400, 12px, ls 0.1em): Genre chip labels. Technical classification, not editorial copy.
- **Catalog** (JetBrains Mono 400, 13px, ls 0.08em): Catalog footer line only (`{year} · {label} · {catalog}`). Slightly larger than Label for room-monitor legibility at 6–10ft — the catalog line is dense metadata that benefits from one step of breathing room above 11px.
- **Label** (JetBrains Mono 400, 11px, ls 0.16em, uppercase): Status strip, PREV/NEXT labels, side/position counter, status indicator. All instrumental metadata. Set in `p.muted`.

### Named Rules

**Empty-state font size exception.** In boot, idle, and error states, the track name hero renders at **48px** (Headline scale) rather than 72px. The placeholder strings ("Listening…", "Waiting for a record", "Couldn't identify") carry different visual weight than a real track name and read better at a reduced scale. All other typography specs in the meta column are unchanged. In production, the artist, album, genre chips, and catalog fields are suppressed in these states — see §5 Idle, Boot, and Error States.

**The Mono Firewall Rule.** JetBrains Mono is for data. Track names, artist names, and album titles are copy — they get Inter Tight or Newsreader. Never set a track name in mono; never set catalog data in a serif.

**The One Serif Rule.** Newsreader italic appears exactly once per artboard: the album title. Adding it anywhere else (credits, footnotes, pull quotes) dissolves the distinction.

## 4. Elevation

The system uses two elevation techniques: **deep ambient shadow** beneath the album cover, and **tonal layering** for surface depth. No borders. No glassmorphism.

The cover shadow (`0 30px 60px rgba(0,0,0,0.55)`) is the only explicit drop shadow in the product. It lifts the cover physically from the background, reinforcing the physical-object vocabulary. Everything else is flat.

Surface depth is conveyed by the `surface` color (slightly lighter than `bg`) used as the radial gradient's inner stop. The gradient originates from the cover side of the layout, so light appears to come from behind the record — consistent with the physical metaphor.

### Shadow Vocabulary
- **Cover Lift** (`0 30px 60px rgba(0,0,0,0.55)`): Beneath the album cover container only. The defining shadow of the system.
- **Cover Ring** (`0 0 0 1px rgba(255,255,255,0.04)`): A hairline inset border on the cover. Keeps the cover edge visible against near-black backgrounds without a visible stroke.
- **Sleeve Frame** (sleeve variant only): `0 30px 60px rgba(0,0,0,0.55)` on the outer container, `0 0 0 1px rgba(0,0,0,0.4) inset` on the inner image.

### Named Rules

**The Flat-Except-Cover Rule.** Shadows belong to the album cover, not to cards, panels, or text elements. The cover is the physical object; it gets lift. Everything else is flat.

## 5. Components

### Status Indicator
The single animated element: an 8×8px circle whose color maps to playback state, with a `pulse` animation during active states. In `playing` and `boot` states, the dot glows (`box-shadow: 0 0 8px accent`). In `paused` and `idle`, it's static.

All animation must include a `@media (prefers-reduced-motion: reduce)` block that sets `animation: none`.

**Animation keyframes (for non-CSS renderers):**
- **Pulse:** `0%/100% { opacity: 1; scale: 1.0 }` → `50% { opacity: 0.55; scale: 0.9 }` — duration 1.6s, ease-in-out, infinite.
- **Rotate:** `from { rotate: 0° }` → `to { rotate: 360° }` — duration 1.4s, linear, infinite.

State color mapping: playing → `accent`; between-tracks → `#e0a040` (warm amber); paused → `muted`; idle → `muted`; boot → `accent`; error → `#c85050` (muted red — ShazamIO failed to identify).

### Boot and Error Arc

Both boot and error empty-cover states render a 72×72px SVG arc centered in the 440×440 cover area. The arc consists of two concentric circles (cx 36, cy 36, r 32, no fill):

1. **Ghost ring:** `stroke: p.muted`, `stroke-opacity: 0.4`, `stroke-width: 1`. Full circle (no dasharray). Provides a stable circular frame so the spinner reads as contained rotation rather than a floating stroke.
2. **Arc segment:** `stroke-width: 1.5`, `stroke-dasharray: "50 200"` (50px visible dash, 200px gap), `stroke-linecap: round`. No fill.
   - **Boot:** arc stroke is `p.accent`. Animated with rotate keyframe (1.4s linear infinite), `transform-origin: 36px 36px`.
   - **Error:** arc stroke is `#c85050` (muted red). Static — no rotation. The stillness is the signal: boot spins; error sits.

The `stroke-dasharray: "50 200"` produces roughly a quarter-circle arc at any rotation angle. Both states share the same SVG geometry; the only differences are stroke color and the presence of the rotate animation.

### Genre Chips
- **Shape:** Nearly square corners (2px radius)
- **Background:** Transparent
- **Text:** `p.muted`, JetBrains Mono 12px, letter-spacing 0.1em, no transform
- **Border:** `1px solid {muted}55` — the muted color at ~33% opacity
- **No hover or active state** — purely informational, non-interactive

### Accent Rule
A 64×2px horizontal rule in `p.accent` at full opacity. Appears between the track name and the artist name. Its width is fixed, not responsive to the column — a deliberate punctuation mark, not a divider that spans the full width.

### Display Layout (DirectionA)
The core artboard: 1024×600px, hard-fixed dimensions. Cover on the left (440×440px), metadata on the right (1fr). Top status strip (30px, suppressed in compact variant). Inset: 60px top, 50px sides, 40px bottom. Grid gap: 44px.

The metadata column orders vertically: display track name → accent rule → headline artist → title album → genre chips (auto-flex to bottom) → catalog footer → PREV/NEXT adjacent panel (when showing adjacent, playing state only).

**Derived geometry:** Metadata column width = 1024 − 50 (left inset) − 440 (cover) − 44 (gap) − 50 (right inset) = **440px**. The cover and metadata column are exactly equal in width. Content area height below the status strip: 600 − 60 (top inset) − 40 (bottom inset) = 500px of usable vertical space per column. The cover is top-aligned within this area; its 440×440px fills roughly 88% of the column height.

**Ambient radial gradient:** A full-artboard gradient layer sits behind the grid and creates the illusion of light emanating from behind the record: `radial-gradient(60% 70% at 25% 35%, {surface} 0%, {bg} 65%)`. The origin is at 25% from the left (cover side) in the default/left layout; 75% in the right/mirror variant. In production renderers without radial gradient support, approximate with a horizontal gradient from `surface` at the cover edge fading to `bg` over ~60% of the artboard width.

In **between** state, a `NEXT` eyebrow label (11px JetBrains Mono, ls 0.22em — slightly wider than the 0.16em Label spec to give the single word more presence, `p.muted`, `marginBottom: 8px`) precedes the track name hero. `trackText` returns `album.next.track` rather than the current track, so the hero still runs at full 72px display scale — the eyebrow carries the contextual "what's coming" signal without demoting the track name to a subtitle role.

### PREV/NEXT Adjacent Panel

An optional context row that appears below the catalog footer line. Shows the tracks immediately before and after the current position on the same side.

- **Visibility:** `playing` state only. Hidden during `between`, `paused`, `idle`, `boot`, and `error`. PREV is hidden when `album.prev` is null (first track on a side). NEXT is hidden when `album.next` is null (last track on a side). The panel is omitted entirely if both prev and next are null.
- **Layout:** Flex row, `marginTop: 12px`, gap 32px. Each track occupies `flex: 1` with `minWidth: 0` (required for text-overflow ellipsis on long track names).
- **Label:** "← PREV" or "NEXT →" — 11px JetBrains Mono 400, ls 0.12em, uppercase, `p.muted`.
- **Track name:** 14px Inter Tight 500, `p.text`, `marginTop: 4px`. Single line, `overflow: hidden; text-overflow: ellipsis; white-space: nowrap`. Note: `letter-spacing: 0` (reset from the parent mono context).
- **Relationship to between state:** The `between` state's NEXT eyebrow is the primary "what's coming next" signal — it appears at the start of the 72px hero during the literal groove gap between tracks. The adjacent panel is a supplementary reference visible while the current track is playing.

### Idle, Boot, and Error States
- **Boot** (`state === 'boot'`): Rotating SVG arc (see Boot and Error Arc above) over `p.surface`. Track name shows "Listening…" at 48px. The cover label is time-progressive: "WARMING UP" (0–19s elapsed), "STILL LISTENING…" (20–59s), "IDENTIFYING… M:SS" (60s+). This lets the room listener distinguish active identification from a hung process without walking to the Pi.
- **Idle** (`state === 'idle'`): Repeating 135° diagonal stripe pattern — stripes 12px wide alternating `p.surface` and `p.bg`. CSS: `repeating-linear-gradient(135deg, {surface} 0 12px, {bg} 12px 24px)`. "NO RECORD ON PLATTER" label at 11px / ls 0.16em. Track name shows "Waiting for a record" at 48px.
- **Error** (`state === 'error'`): Static arc (see Boot and Error Arc above) over `p.surface`. "NO MATCH FOUND" primary label + "REPOSITION NEEDLE TO RETRY" recovery hint, both at 11px Label scale. Track name shows "Couldn't identify" at 48px. Signals that ShazamIO completed but found no matching release — distinct from boot (still trying) and idle (no audio).

**Production behavior:** All three states replace the cover image with the empty cover component and show no album metadata. The track name shows a state-specific string ("Listening…", "Waiting for a record", "Couldn't identify"); artist, album, genre chips, and catalog footer are suppressed. When the system enters idle cold (no album ever loaded), these fields have no data to display. When it enters boot or error after a prior album, displaying stale metadata from the previous record would be misleading.

**Prototype behavior:** `DirectionA.jsx` always renders the full meta column regardless of state. Sample data is always present in the prototype, so the fields are never undefined — suppression is not needed for design-review purposes. The cover is replaced with `DirAEmptyCover`, and the track name text changes, but artist/album/chips/catalog remain visible. This is intentional for the prototype: it lets you evaluate the full layout for each state with real album data in the meta column. The production Python renderer should implement the suppression described above.

**Idle screen is a v1.4.0 design gap.** The diagonal stripe placeholder is intentionally minimal and temporary. The planned redesign will show a grid of recently played album covers, optional clock/date, and a random Discogs collection suggestion during extended idle. Any idle screen design must: use the fallback palette, preserve sharp-corner vocabulary, and feel like a continuation of the room monitor — not a different product.

## 6. Do's and Don'ts

### Do:
- **Do** use the `muted` role at full opacity for secondary text. Its color already communicates subdued; additional opacity stacks invisibly against dark backgrounds and fails contrast.
- **Do** enforce ≥60° hue separation between any two albums' `accent` colors in `data.js`. Unconstrained extraction clusters toward warm orange; the collection needs to span the hue wheel.
- **Do** set `alt` on album cover images to `"{artist} — {album}"`. The cover is the primary visual element; marking it decorative (`alt=""`) erases it from screen readers.
- **Do** include `@media (prefers-reduced-motion: reduce)` on every `@keyframes` animation. The pulse and rotate animations are continuous; they need static fallbacks.
- **Do** protect long album titles from overflow. The `title` level (32px Newsreader italic) needs `overflow: hidden` and `-webkit-line-clamp: 2` or equivalent; "Bachelor No. 2 or, the Last Remains of the Dodo" at 32px will overflow without it.
- **Do** keep the five palette roles semantic (`bg`, `surface`, `accent`, `text`, `muted`). Adding album-specific tokens outside this structure breaks the theming architecture.
- **Do** protect long track names from overflow. The `display` level (72px Inter Tight) needs `overflow: hidden` and `-webkit-line-clamp: 3`; production track names can be long.
- **Do** cap genre chip display at 3 with a `+N` overflow indicator, and set `minHeight: 28` on the chip container. Discogs can return 0 or 5+ genres; the catalog footer must stay anchored regardless.

### Don't:
- **Don't** introduce the streaming-app aesthetic: no rounded album tiles, no progress bars, no playhead dots, no shuffle/repeat/heart icons. This display has no playback controls and should not imply any.
- **Don't** show elapsed time or track duration. The display is static by design — vinyl is not an experience you track. Showing progress would imply the listener should be watching the screen.
- **Don't** use generic music visualization decoration (waveform bars, equalizer visualizers, pulsing blobs). These are decoration masquerading as information.
- **Don't** add rounded corners to the artboard, cover container, or metadata panel. The display is architectural; corners are square. Genre chips allow 2px only.
- **Don't** set album title or track name in JetBrains Mono. The mono firewall is semantic: mono for data, Inter Tight for identity, Newsreader for warmth.
- **Don't** add a second serif-italic element anywhere. Newsreader italic earns its warmth precisely because it appears once. Repeating it anywhere collapses the contrast.
- **Don't** apply `border-left` or `border-right` as a colored stripe on any element. The accent rule is a bottom-inset horizontal rule of fixed width, not a side stripe.

## 7. Album Data Schema

The production renderer receives an album object per identified track. All production data originates from the three-tier metadata chain: Discogs collection (pressing-specific) → Discogs database → MusicBrainz fallback.

### Required fields

| Field | Type | Description |
|---|---|---|
| `artist` | string | Artist display name |
| `album` | string | Album title. May be very long — protect with 2-line clamp at 32px |
| `cover` | string | Cover image path or URL (440×440 ideal). Production source: Discogs release art or locally cached file |
| `track` | string | Currently playing track name. May be long — clamp to 3 lines at 72px (48px in empty states) |
| `side` | string | Record side: `"A"`, `"B"`, `"C"`, `"D"` |
| `position` | integer | 1-based track number on this side |
| `sideTracks` | integer | Total track count on this side |
| `year` | integer | Original release year (4-digit, e.g. 1987) |
| `label` | string | Record label name (e.g. `"SST Records"`) |
| `catalog` | string | Pressing catalog number (e.g. `"SST-134"`) |
| `genres` | string[] or null | Genre tags from Discogs. May be an empty array or null — guard with `(genres ?? [])`. Display shows max 3 + `+N` overflow indicator. |
| `palette` | object | Extracted display palette: `{ bg, surface, accent, text, muted }` — all hex strings. See §2 for extraction rules. |

### Optional fields

| Field | Type | Description |
|---|---|---|
| `prev` | object or null | Previous track on this side. **Null for the first track on a side.** Shape: `{ track: string, side: string, position: integer }`. |
| `next` | object or null | Next track on this side. **Null for the last track on a side.** Shape: `{ track: string, side: string, position: integer }`. Also the source for the `between` state hero: `next.track`. |
| `duration` | integer | Track duration in seconds. **Not rendered** — the display is intentionally static. Retained for potential future use. |
| `elapsed` | integer | Playback position in seconds. **Not rendered** for the same reason. |

### Null album context

During `idle` and `boot` states (before any album is identified), and during `error` state (ShazamIO found no match), the album object may be null or absent. The production renderer must handle null gracefully:

- Show the appropriate empty cover (`DirAEmptyCover` / boot arc / stripe / error arc)
- Show the state-appropriate track name placeholder at 48px
- Suppress all album metadata fields: artist, album, genres, catalog footer, PREV/NEXT panel
- Use the fallback palette (see §2)
