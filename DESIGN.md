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

State color mapping: playing → `accent`; between-tracks → `#e0a040` (warm amber); paused → `muted`; idle → `muted`; boot → `accent`; error → `#c85050` (muted red — ShazamIO failed to identify).

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

The metadata column orders vertically: display track name → accent rule → headline artist → title album → genre chips (auto-flex to bottom) → catalog footer → PREV/NEXT context (when showing adjacent).

In **between** state, a `NEXT` eyebrow label (11px Label scale, `p.muted`, `marginBottom: 8px`) precedes the track name hero. `trackText` returns `album.next.track` rather than the current track, so the hero still runs at full 72px display scale — the eyebrow carries the contextual "what's coming" signal without demoting the track name to a subtitle role.

### Idle, Boot, and Error States
- **Boot** (`state === 'boot'`): Rotating SVG arc in `p.accent` over a `p.surface` background. Progressive elapsed label: "WARMING UP" (0–20s), "STILL LISTENING…" (20–60s), "IDENTIFYING… M:SS" (60s+). Track name shows "Listening…".
- **Idle** (`state === 'idle'`): Repeating 135° diagonal stripe pattern (`p.surface` / `p.bg`). "NO RECORD ON PLATTER" label. Track name shows "Waiting for a record".
- **Error** (`state === 'error'`): Static (non-rotating) SVG arc in `#c85050` (muted red) over `p.surface`. "NO MATCH FOUND" primary label + "REPOSITION NEEDLE TO RETRY" recovery hint, both at 11px Label scale. Track name shows "Couldn't identify". Signals that ShazamIO completed but found no matching release — distinct from boot (still trying) and idle (no audio).

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
