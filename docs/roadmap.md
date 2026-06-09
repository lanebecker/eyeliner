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

## v1.2.0 — Display Redesign & Dynamic Theming ✅

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

### v1.2.1 ✅

**Dynamic title push-down layout** — the track title is no longer confined to a
fixed-height slot. It claims as much vertical space as it naturally requires, and
the accent divider, artist name, album title, and genre chip badges flow downward
from the title's actual bottom edge. The meta footer and prev/next strip stay
bottom-anchored. Font size reduction is a genuine last resort, applied only when
the title cannot fit even with the full available budget.

- `_draw_wrapped_text()` now returns actual rendered height in pixels
- New `_measure_wrapped_text()` helper for layout-safe height pre-computation
- `_draw_genre_chips()` accepts an optional `chips_rect` override for dynamic positioning
- New `_bold_fonts` dict pre-built at startup with stepped-down bold title sizes

### v1.2.2 ✅

**Cross-side boundary fix for `prev_track_title` / `next_track_title`** — the
side-awareness properties now correctly stitch across side boundaries. Previously,
the first track on any side (e.g. B1) returned `None` for `prev_track_title`
instead of falling back to the last track of the preceding side (A3), and the
last track on any side (e.g. A3) returned `None` for `next_track_title` instead
of the first track of the following side (B1). The global tracklist is now
consulted as a fallback whenever a side boundary is hit.

- 193-test unit suite (+1 test; also expanded cross-side coverage with renamed tests)

---

## v1.3.0 — Last.fm Scrobbling ✅

**208-test unit suite** (+15 tests in `tests/test_lastfm_client.py`).

**What shipped:**
- New `src/tracking/lastfm_client.py` — wraps `pylast` (Last.fm Scrobbling
  API 2.0). Synchronous; async callers use `run_in_executor`. Graceful no-op
  when not configured or when `pylast` is not installed. All failures caught
  internally — no exception ever propagates out.
- Per-track scrobble fired from `RecognitionLoop._commit_track()` when a track
  is confirmed. Scrobble includes timestamp, artist, title, and album.
- Optional "Loved" mark when `ListenTracker._end_session()` fires with
  `potential_last_track = True` — configurable via `love_on_completion`,
  off by default.
- `get_lastfm_session_key.py` — one-time helper at the repo root that walks
  through the desktop auth flow and prints the session key to paste into
  `config.yaml`. Session keys do not expire.
- New `lastfm` section in `config.example.yaml`:
  `scrobble_enabled`, `api_key`, `api_secret`, `session_key`, `love_on_completion`.
- `pylast>=5.1.0` added to `requirements.txt`.

### v1.3.1 ✅

**Bug-fix release — no new features.** Post-QA sweep of the entire codebase
identified eight bugs across five files; all fixed with zero test regressions
(208 tests still pass).

- Seven `asyncio.get_event_loop()` → `asyncio.get_running_loop()` replacements
  across `capture.py`, `recognizer.py`, `listen_tracker.py`, and `main.py`
  (deprecated in Python 3.10+; raises `RuntimeError` in some async contexts)
- ShazamIO album extraction: inner `break` only exited the metadata loop, not
  the outer sections loop — albums from non-first sections were silently missed
- Cover-art download (`urlretrieve`) was blocking the async event loop
  synchronously on every new track; refactored to an async prefetch via
  `run_in_executor` + `create_task`
- Pulsing NOW PLAYING dot froze after ~1 second (`_dirty` never re-set)
- Genre chip overflow check allowed a full extra row beyond the panel boundary
- NEXT track label used `(*p.text[:3],)` instead of `p.text` (inconsistent
  with the PREV label)
- Wrong Last.fm auth URL in help text of `get_lastfm_session_key.py`
  (`/api/accounts` → `/api/account/create`)
- Negative sleep duration possible in `AudioCapture.run()` when
  `overlap_seconds >= chunk_seconds`; clamped with `max(0, ...)`

### v1.3.2 ✅

**Bug-fix release — no new features.** A follow-up QA sweep of the v1.3.1
codebase found one site the previous sweep had missed plus several other
real bugs and hardening opportunities.

- Completed the `get_event_loop()` → `get_running_loop()` migration for the
  eighth and final site (`resolver.py`), which v1.3.1 missed
- Fixed the dirty-flag clobber that froze the pulsing NOW PLAYING dot and
  the IDENTIFYING spinner after the initial palette transition (the v1.3.1
  fix did not actually take effect)
- Tightened `PlaySession.log_track` to refuse `DISCOGS_DATABASE` results
  (release_id but no instance_id), preventing a guaranteed-to-fail Discogs
  POST to `…/instances/None/fields/…` when only DB metadata was available
- Renamed and flipped the misleading
  `test_database_source_without_instance_id_does_not_increment` test that
  documented the bug rather than catching it
- Added HTTP timeouts on every Discogs API call and a timeout + atomic
  rename on the cover-art download
- Improved diagnostics: multi-match logging for `AudioCapture._find_device_index`,
  DEBUG-level log when a recognition chunk is dropped
- Normalized case + whitespace when comparing recognition results, so
  Shazam formatting jitter doesn't trigger spurious re-commits
- Bounded `_palette_cache` to 200 entries (LRU-ish eviction)
- Snap `_current_palette` to the live interpolated value on mid-transition
  track changes, so the new lerp doesn't jump back to a stale start
- Adaptive render cadence (30 fps during palette transition, 10 fps
  otherwise) for the pulsing dot animation, easing CPU load on the Pi
- Fixed several documentation inaccuracies (CLAUDE.md `discogs.token`,
  outdated PlaySession description, architecture-diagram LastFmClient
  placement, current-state test count)
- README now mentions the `venv` setup step
- `sync-version-badge.yml` regex now handles hyphenated pre-release
  versions like `1.4.0-rc1`
- 210-test unit suite (+2 model-level regression tests covering the new
  latching behavior)

### v1.3.3 ✅

**Bug-fix and performance release — no new features.** A full-codebase deep
review (the first conducted with Claude Fable 5) found and fixed one real
notification bug, one capture-pipeline design flaw, and a series of
performance and asyncio-hygiene issues. See `CHANGELOG.md` for full detail.

- `PlayerState.set_track()` now notifies listeners on every track change —
  previously every track after the first was silently swallowed, so the
  renderer never prefetched new cover art or queued palette transitions
  mid-session
- Capture redesigned around continuous `sd.InputStream` + a new pure-numpy
  `ChunkAssembler` (`src/audio/chunking.py`) — the old record-then-sleep
  loop's "5s overlap" was actually a 10s dead gap between chunks
- Album-level metadata cache in `MetadataResolver` (~90% fewer Discogs
  requests per LP) and 429/`Retry-After` rate-limit handling in
  `DiscogsClient`
- Render-loop hot paths cached: scaled cover art (was re-decoding a JPEG
  ~10×/second) and the radial gradient background (was 24 full-screen
  circle fills per frame), via a shared `_BoundedCache` helper
- Clean Ctrl+C shutdown (cancellation-based; no more `RuntimeError`
  traceback) and strong references for fire-and-forget asyncio tasks
- Shazam client reused across recognitions instead of rebuilt per chunk
- 261-test unit suite (+51: first-ever `PlayerState` tests, ChunkAssembler
  windowing tests, renderer cache/color tests, resolver cache tests,
  Discogs rate-limit tests)

### v1.3.4 ✅ (current)

**Behavior-refinement release.** Implements the design decisions deferred
from the v1.3.3 deep review; see `CHANGELOG.md` for full detail.

- `is_last_track` matches by tracklist position instead of title — closes
  the duplicate-title phantom-play-count hole (conservative residual:
  a genuine closer with a duplicated title is missed, never phantom-counted)
- Sessions auto-split when a confirmed track resolves to a different
  Discogs release than the latched one (record swapped in under 45s);
  record 1 is still credited if its closer played
- Side flips keep the now-playing card on screen (LISTENING is entered
  only from IDLE); the card updates in place when the next track commits
- Removed dead code: the never-set `PlayerStatus.SESSION_ENDED` enum value
  and `ListenTracker`'s unused `config` constructor parameter
- Scrobble timing reviewed and deliberately left as-is (timestamps run
  ~25s behind track start — a documented rounding error, not a bug)
- 271-test unit suite (+10: album-change splits, position-based
  last-track matching)

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
