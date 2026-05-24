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

## v1.1.0 — Discogs Listening Statistics ✅ (current)

- Writes a "Last Played" date (ISO 8601, `YYYY-MM-DD`) to a configurable Discogs
  custom field each time a full album side plays through.
- Field is optional: if `discogs.last_played_field_name` is not set in `config.yaml`,
  the method is a graceful no-op and no API calls are made.
- New `discogs.last_played_field_name` config key (optional).
- 148-test unit suite (+10 tests covering `update_last_played` and its
  `ListenTracker` integration).

---

## v1.2.0 — Last.fm Scrobbling

**Why next:** highest value-to-effort ratio of any remaining feature on the
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

## v1.3.0 — Idle Screen & Recent Plays

**Why third:** the idle screen is currently a blank dark background (a TODO in
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

## v1.4.0 — Side A / Side B Awareness

**Why fourth:** makes the listening completion logic meaningfully more accurate.
Right now the tracker treats every needle-drop-to-lift session the same way,
so playing only Side A of a two-sided album can still trigger a Play Count
increment if Side A happens to end on the last listed track. Side awareness closes
that gap and enables the flip reminder.

**What it adds:**
- `PlaySession` gains a `side` field inferred from the track positions identified
  (`"A"` if all tracks are Ax, `"B"` if all are Bx, `None` if mixed or unknown)
- Play Count increment now requires both Side A and Side B sessions to have
  completed — stored in a small local state file between sessions
  (e.g. `~/.vinyl-now-playing/session-state.json`)
- Idle screen "Flip to Side B →" reminder after a Side A session ends
- `ListenTracker` gains a `_completed_sides` dict keyed by `release_id`

---

## v1.5.0 — Album Art Color Theming

**Why fifth:** mostly cosmetic but genuinely delightful. Every record gets its
own visual identity on screen. `Pillow` is already in `requirements.txt`.

**What it adds:**
- Extract the dominant colour from the cached album art JPEG using
  `Pillow`'s colour quantization
- Dynamically set `DisplayRenderer.accent_color` (currently used for the track
  title) to the extracted dominant colour, with a luminance clamp so it's always
  readable against the dark background
- Smooth transition between colours when the track changes (lerp over ~1 second)
- New `display.dynamic_accent_color` config boolean (default `true`)

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
