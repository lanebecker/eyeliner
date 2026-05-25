# Feature Roadmap — vinyl-now-playing

Versioning follows [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.
Minor releases add features; patch releases fix bugs without changing behaviour.
The current version is in the `VERSION` file at the repo root.

---

## v1.0.0 — Foundation ✅

The complete core loop: turntable audio → Shazam recognition → Discogs metadata
→ pygame display → Discogs field update on album completion.

- Full async audio pipeline (AudioCapture → SilenceDetector → RecognitionLoop)
- ShazamIO recognition with N-of-consecutive-matches confirmation gate
- Three-tier metadata resolution: Discogs collection → Discogs database → MusicBrainz fallback
- Proportional pygame display layout at any resolution (primary: Waveshare 7" HDMI, 1024×600)
- Discogs collection search with 25-candidate + collection-walk fallback strategy
- End-to-end Discogs field update on album completion
- 124-test unit suite covering all non-hardware components

### v1.0.1 ✅

- Replaced the boolean "Listened?" field update with a **Play Count increment**:
  reads the current integer value, increments by 1, writes back. Empty Play Count
  implies unlistened — no separate boolean field needed.
- New `discogs.play_count_field_name` config key replaces `listened_field_name`
  and `listened_field_value`.

---

## v1.1.0 — Discogs Listening Statistics ✅

- Writes a "Last Played" date (ISO 8601, `YYYY-MM-DD`) to a configurable Discogs
  custom field each time a full album side plays through.
- Field is optional: if `discogs.last_played_field_name` is not set in `config.yaml`,
  the method is a graceful no-op and no API calls are made.
- New `discogs.last_played_field_name` config key (optional).
- 148-test unit suite (+10 tests covering `update_last_played` and its
  `ListenTracker` integration).

---

## v1.2.0 — Display Redesign & Dynamic Theming ✅ (current)

A comprehensive visual overhaul based on the "Museum Card" layout from Claude
Design mockups. Absorbed the color theming work originally planned for v1.5.0
and laid the tracklist-parsing groundwork that makes v1.5.0's Side A/B behavioral
logic significantly lighter to implement.

**192-test unit suite** (+44 tests covering side-awareness properties, new layout
geometry, and genres passthrough).

**What shipped:**

*Layout redesign:*
- Track name promoted to the dominant visual element — displayed at maximum size
  at the top of the text panel, where the artist name currently lives
- Short horizontal accent divider line between track name and artist
- Artist name rendered large and bold beneath the divider
- Album name rendered in italic serif in the accent color (below artist)
- Genre and style tags from Discogs displayed as small bordered pill badges
- Slim full-width header bar: `● NOW PLAYING` indicator (left) and
  `SIDE A · 04 OF 06` track position (right), both in small monospace
- Two-column footer showing `← PREV` / previous track name and
  `NEXT →` / next track name

*Dynamic color theming (absorbed from planned v1.5.0):*
- Dominant color extracted from the cached album art JPEG using Pillow's color
  quantization
- Background color subtly tinted per album, giving each record a distinct visual
  atmosphere
- Accent color (divider line, album name, genre badge borders) derived from the
  same extraction, luminance-clamped for readability against the dark background
- Smooth color transition when the track changes (lerp over ~1 second)
- New `display.dynamic_theming` config boolean (default `true`)

*Tracklist parsing (foundation for v1.5.0 side awareness):*
- Full tracklist fetched from the existing Discogs release response — no new API
  calls; the data is already returned, just not previously extracted
- Track positions parsed to determine current side (`A`, `B`, etc.), position
  within that side, and total tracks on the side
- Previous and next track names resolved from position in tracklist
- Genre and style arrays extracted from the same release response

**Files affected:** `layouts.py` (full geometry redesign), `renderer.py` (pill
badges, divider line, header bar, prev/next footer, color extraction and
transition logic), `models.py` (extended `TrackMetadata` with genres and
side-awareness properties; added `DisplayPalette`, `FALLBACK_PALETTE`,
`_SIDE_RE`), `discogs_client.py` (surface genres and styles from existing
response), `resolver.py` (genres passthrough)

**No new API calls.** All new data surfaces come from fields already present in
the Discogs release response. Color extraction runs locally on the cached album
art image.

---

## v1.3.0 — Last.fm Scrobbling

**Why third:** highest value-to-effort ratio of any remaining feature on the
list. Last.fm builds a permanent, queryable listening history. For vinyl
listeners this is especially satisfying — nothing else does it automatically.
The `ListenTracker` already has the right hooks; this is mostly a new client
module.

**What it adds:**
- New `src/tracking/lastfm_client.py` — wraps the Last.fm Scrobbling API 2.0
- Per-track scrobble fired from `RecognitionLoop._commit_track()` when a track
  is confirmed (includes timestamp, artist, title, album)
- Optional "loved" mark when `ListenTracker._end_session()` fires and
  `potential_last_track` is True (configurable, off by default)
- New `lastfm` section in `config.yaml`: `api_key`, `api_secret`, `session_key`

**Config addition:**
```yaml
lastfm:
  api_key: "YOUR_API_KEY"
  api_secret: "YOUR_API_SECRET"
  session_key: "YOUR_SESSION_KEY"   # generated once via auth flow
  scrobble_enabled: true
  love_on_completion: false
```

---

## v1.4.0 — Idle Screen & Recent Plays

**Why fourth:** the idle screen is currently a blank dark background (a TODO in
`DisplayRenderer._render_idle()`). This is the most visible gap in the daily
experience — the Pi is on all the time, and "nothing" is what you see most.

**What it adds:**
- Grid of recently played album covers (pulled from session history or a small
  local log) on the idle screen
- Optional clock / date display
- "What to play tonight?" — a random record from your Discogs collection
  displayed during extended idle periods (calls `DiscogsClient` to pull a
  random collection item)
- New `display.idle_mode` config option: `"recent"` | `"random"` | `"clock"` | `"blank"`

---

## v1.5.0 — Side A / Side B Awareness

**Why fifth:** makes the listening completion logic meaningfully more accurate.
Right now the tracker treats every needle-drop-to-lift session the same way,
so playing only Side A of a two-sided album can still trigger a Play Count
increment if Side A happens to end on the last listed track. Side awareness closes
that gap and enables the flip reminder. The tracklist parsing introduced in
v1.2.0 means this version can focus purely on behavioral logic — no parsing
groundwork required.

**What it adds:**
- `PlaySession` gains a `side` field inferred from the track positions identified
  (`"A"` if all tracks are Ax, `"B"` if all are Bx, `None` if mixed or unknown)
- Play Count increment now requires both Side A and Side B sessions to have
  completed — stored in a small local state file between sessions
  (e.g. `~/.vinyl-now-playing/session-state.json`)
- Idle screen "Flip to Side B →" reminder after a Side A session ends
- `ListenTracker` gains a `_completed_sides` dict keyed by `release_id`

---

## v1.6.0 — Local Web Dashboard

**Why sixth:** optional quality-of-life for multi-room or phone-checking use
cases. Runs alongside the main app as a lightweight HTTP server.

**What it adds:**
- Minimal `aiohttp` web server running on `:8080` (or configurable port)
- `GET /` — now-playing page: cover art, artist, album, track, source badge
- `GET /api/now-playing` — JSON endpoint for the current `PlayerState`
- Auto-refreshes every 10 seconds via `<meta http-equiv="refresh">`
- Accessible from any device on the local network at `http://vinylpi.local:8080`
- New `web.enabled` and `web.port` config keys

---

## Unscheduled / under consideration

These are interesting but don't yet have a clear version slot:

**Spotify "find on streaming"** — when a track is identified, generate a
Spotify search URL (`https://open.spotify.com/search/...`) and display it as a
QR code on the idle screen. Zero API key required.

**BlueSky / Mastodon posting** — optional "now playing" post when a new track
commits, using the AT Protocol or Mastodon API. Opt-in, obviously.

**Per-record listening journal** — a local markdown file (or sqlite DB) logging
every play session with timestamps, tracks identified, and whether the full side
completed. Useful even without Discogs.

**AudD / ACRCloud backend** — the `RecognizerBackend` ABC is already in place;
these are drop-in alternatives to ShazamIO for users who want a commercial
recognition service with higher accuracy or rate limits.

**Hardware buttons** — GPIO-connected physical buttons on the Pi for
display brightness, skip to idle screen, or triggering a "loved" mark
without touching the Discogs web UI.
