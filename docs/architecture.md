# Architecture — vinyl-now-playing

A Raspberry Pi 4 listens to a turntable via a USB audio interface, identifies
tracks using Shazam, looks up pressing details in Discogs, displays the
now-playing info on an HDMI screen, scrobbles each track to Last.fm, increments
the Play Count, and records the Last Played date in your Discogs collection when
a full side plays through.

---

## System diagram

```
Turntable (RCA) → Behringer UCA222 (USB) → Raspberry Pi 4
                                                  │
                                         AudioCapture (sounddevice
                                         InputStream → ChunkAssembler)
                                         15s chunks every 10s, 5s true overlap
                                                  │
                              ┌───────────────────┴────────────────────┐
                              ▼                                         ▼
                     SilenceDetector                          RecognitionLoop
                     (RMS threshold)                          (ShazamIO backend)
                              │                                         │
                   AudioEvent (enum)                        RawRecognitionResult
                   MUSIC_STARTED                            (title, artist, album)
                   MUSIC_STOPPED                                        │
                   SESSION_ENDED                              confirmation gate
                              │                               (N consecutive matches)
                    ┌─────────┴──────────┐                             │
                    ▼                    ▼                    MetadataResolver
               PlayerState         ListenTracker              3-step lookup:
               (status +           (PlaySession)           1. Discogs collection
               current_track)           │                  2. Discogs database
                    │            SESSION_ENDED              3. MusicBrainz fallback
                    │                   │                             │
                    │                   ├─► DiscogsCollectionWriter   ▼
                    │                   │   .increment_play_count   TrackMetadata
                    │                   │   .update_last_played        │
                    │                   │                       ┌──────┴──────────┐
                    │                   └─► LastFmClient        ▼                 ▼
                    ▼                       .love(last_track)  PlayerState   ListenTracker
             DisplayRenderer                (on completion)    .current_track .log_track()
             (pygame, HDMI)                                                       │
                                                                                  ▼
                                                                          LastFmClient
                                                                          .scrobble()
                                                                       (per committed track)
```

---

## Component reference

### `main.py` — Entry point

Instantiates all components, wires up event listeners, and runs the async
event loop. It is the **composition root**: `load_config()` returns a typed
`AppConfig`, and `main.py` builds one shared `DiscogsHttp`, injects a
`DiscogsReader` into `MetadataResolver(reader)` and a `DiscogsCollectionWriter`
into `ListenTracker(writer, lastfm)`, and constructs
`TrackCommitService(state, resolver, tracker, lastfm)` — passing
`commit_service.commit` to `RecognitionLoop` as its `on_confirmed` callback.
Key wiring:

- `SilenceDetector.on_event` → `ListenTracker.on_silence_event` + `PlayerState.set_status`
- `RecognitionLoop` confirms a track → `on_confirmed` (= `TrackCommitService.commit`) → `MetadataResolver.resolve` → `PlayerState.set_track` + `ListenTracker.on_track_identified` + Last.fm scrobble
- `PlayerState.on_change` → `DisplayRenderer._on_state_change` (sets dirty flag)

**Graceful shutdown (v1.3.5):** the three pipeline coroutines run as named
tasks awaited with `asyncio.wait(return_when=FIRST_COMPLETED)` — the moment
ANY leg exits (the display closing on ESC/window-close, an unexpected
coroutine death, or `SIGINT`/`SIGTERM` cancelling everything), the remaining
legs are cancelled and `main()` unwinds through a `finally` block that calls
`capture.stop()` and `display.stop()`, after which `asyncio.run()` exits
cleanly.  History: pre-v1.3.3 called `loop.stop()` inside `asyncio.run()`
(guaranteed `RuntimeError` traceback on Ctrl+C); v1.3.3's cancellable gather
fixed that but waited for ALL legs, so quitting the display via ESC left
capture and recognition running headless forever (fixed in v1.3.5).

---

### `src/audio/capture.py` — AudioCapture

Records **continuously** from the USB interface using `sounddevice` and emits
genuinely overlapping chunks (redesigned in v1.3.3 — the previous
record-then-sleep approach left a 10s dead gap between "overlapping" chunks).

**Three-stage pipeline:**
1. `sd.InputStream` records without interruption; its PortAudio callback
   (running on a non-asyncio audio thread) hands each ~0.25s block to the
   event loop via `loop.call_soon_threadsafe`
2. `run()` drains blocks from an `asyncio.Queue` (drop-oldest policy at 64
   blocks ≈ 16s of slack) and feeds them to a `ChunkAssembler`
   (`src/audio/chunking.py`), which emits a `chunk_seconds`-long window every
   `chunk_seconds - overlap_seconds`
3. Each emitted chunk is dispatched synchronously to
   `SilenceDetector.process()` and asynchronously enqueued for `RecognitionLoop`

**Key behaviour:**
- Finds the device by name substring match against `audio.device_name` in config
- Default timing: a 15s mono float32 window at 44100 Hz every 10s, with a true
  5s shared region between consecutive windows — no audio is ever unheard
- `overlap_seconds >= chunk_seconds` is rejected at startup (warning logged,
  overlap disabled) rather than silently misbehaving
- Frame counts are coerced to whole integers (v1.3.5) — fractional seconds in
  config.yaml previously crashed mid-capture as float numpy slice indices;
  ChunkAssembler also validates integrality as a second line of defence
- On stream errors, retries with a fresh `InputStream` after 1s; cancellation
  propagates cleanly for shutdown

**Config keys:** `audio.device_name`, `audio.sample_rate`, `audio.chunk_seconds`,
`audio.overlap_seconds`

---

### `src/audio/chunking.py` — ChunkAssembler

Pure-numpy rolling-window chunker (new in v1.3.3): feed arbitrary-size audio
blocks, receive `chunk_frames`-long windows every `hop_frames`
(hop = chunk − overlap).  Chunk N starts at frame N×hop, so consecutive
chunks share exactly (chunk − hop) frames.  Emitted chunks are independent
copies; the internal buffer stays bounded below `chunk_frames` between feeds.
No sounddevice dependency — fully unit-testable without audio hardware
(`tests/test_chunking.py`).

---

### `src/audio/silence.py` — SilenceDetector

Classifies each audio chunk as music or silence using RMS energy, then fires
lifecycle events.

**RMS calculation:** `float(np.sqrt(np.mean(audio ** 2)))`

**State machine:**

```
[startup]
    │
    ├── chunk RMS >= threshold  ──→  _is_music=True   → emit MUSIC_STARTED
    │
[music playing]
    │
    ├── chunk RMS < threshold   ──→  _is_music=False
    │                                _silence_since = now
    │                                emit MUSIC_STOPPED
    │
[silence after music]
    │
    ├── chunk RMS >= threshold  ──→  back to [music playing], _session_ended=False
    │
    └── elapsed >= session_end_silence_seconds
        AND _session_ended == False   ──→  _session_ended=True, emit SESSION_ENDED
```

**Guards:**
- `SESSION_ENDED` fires at most once per session (`_session_ended` latch)
- `SESSION_ENDED` cannot fire before any music plays (`_silence_since` is None at startup)
- New music after `SESSION_ENDED` resets `_session_ended=False`, enabling a
  second `SESSION_ENDED` for the next session

**Events emitted (`AudioEvent` enum):**

| Event | Meaning |
|-------|---------|
| `MUSIC_STARTED` | First above-threshold chunk after silence |
| `MUSIC_STOPPED` | RMS drops below threshold (inter-track gap or lift) |
| `SESSION_ENDED` | Silence sustained for `session_end_silence_seconds` |

**Config keys:** `audio.silence_threshold_rms` (default 0.01),
`audio.session_end_silence_seconds` (default 45)

---

### `src/audio/recognizer.py` — RecognitionLoop

Polls for track identity while music plays, with a confirmation gate to prevent
flickering on noisy or one-off wrong results.

**Components:**

`RawRecognitionResult` — minimal dataclass returned by any backend:
`title`, `artist`, `album`, `isrc` (optional), `confidence` (optional)

`RecognizerBackend` — ABC with one method: `async recognize(audio, sample_rate) → Optional[RawRecognitionResult]`

**Queue policy (v1.3.5):** chunks arrive from AudioCapture via a bounded
`asyncio.Queue` (maxsize 5).  When the backend lags and the queue fills, the
OLDEST chunk is evicted and the incoming one admitted — the freshest audio is
the most relevant for detecting a track change, matching AudioCapture's
block-queue policy.

`ShazamIOBackend` — default backend:
- Serialises the numpy audio array to an in-memory WAV via `soundfile`
- Passes bytes to `shazamio.Shazam.recognize()` — the `Shazam` client is
  created once on first use and reused for every recognition (v1.3.3)
- Extracts album from the `sections[].metadata[]` block where `title == "album"`

**Confirmation logic (`_handle_result`):**
1. If the result matches the currently playing track → skip (no re-commit)
2. If the result matches `_pending_result` → increment `_pending_count`
3. Otherwise → reset: `_pending_result = result`, `_pending_count = 1`
4. If `_pending_count >= confirmation_required` → `await on_confirmed(result)`, reset pending state

After A-9 the loop holds only `state` (for dedup + miss-counting → ERROR); it no
longer knows about the resolver, tracker, or Last.fm. The confirmed result is
handed to an injected `on_confirmed` callback — wired in `main.py` to
**`TrackCommitService.commit`** (`src/app/track_commit_service.py`), which owns
the commit sequence:
- Records a Unix timestamp (`int(time.time())`) and captures the session epoch
  before the blocking resolve (B-1 guard)
- `await resolver.resolve(raw)` — full metadata lookup
- If a SESSION_ENDED bumped the epoch during the resolve await → discard the
  commit (the needle lifted; don't resurrect a dead track)
- `state.set_track(metadata)` **then** `state.set_raw(raw)` — set_track before
  set_raw so `current_raw` never leads a failed commit (B-11)
- `await tracker.on_track_identified(metadata)` — logs to the play session
- `lastfm.scrobble(metadata, timestamp)` — fires in an executor (non-blocking); failure is caught and logged, never interrupts the loop

**Config keys:** `recognition.backend` (default `"shazamio"`),
`recognition.confirmation_required` (default 2),
`recognition.error_after_misses` (default 6),
`recognition.poll_interval_seconds` (required)

---

### `src/metadata/resolver.py` — MetadataResolver

Orchestrates the three-step metadata lookup chain. Always returns a
`TrackMetadata` — downstream components never need to handle None.

**Lookup order:**

| Step | Source | `TrackMetadata.source` | Notes |
|------|--------|------------------------|-------|
| 1 | User's Discogs collection | `DISCOGS_COLLECTION` | Includes pressing details, instance_id |
| 2 | Discogs database | `DISCOGS_DATABASE` | Generic release metadata, no instance_id |
| 3 | Shazam raw + MusicBrainz cover art | `FALLBACK` | Minimal metadata |

Each step runs via `asyncio.run_in_executor` (blocking API calls). The resolve
boundary uses the transient-vs-permanent error taxonomy in
`src/metadata/errors.py` (`is_transient`): a transient/network failure is logged
at info as "couldn't determine" and leaves the album uncached/retryable, while
an unexpected error is logged loudly — so a transient blip never pins an owned
album to a fallback downgrade for the session.

**Album-level result cache (v1.3.3):** a full Discogs resolve can cost 30+
HTTP requests, and every track on an album shares the same (artist, album)
pair, so `resolve()` caches per normalized `(artist.lower(), album.lower())`
key.  Discogs hits cache the result dict + source tier; fallback results
cache the cover-art URL — but only when both Discogs tiers completed without
raising, so a transient network error never pins an album to fallback
metadata for the session.  Bounded at 64 albums (`_ALBUM_CACHE_MAX`) with
LRU-style eviction.  Cuts per-LP Discogs traffic by roughly 90%.

---

### `src/metadata/discogs/` — Discogs access (transport / reader / writer)

The former `DiscogsClient` God object was split (A-4) into three single-purpose
collaborators that share one HTTP transport. The resolver depends only on the
reader; the tracker depends only on the writer; `main.py` builds one transport
and injects each half.

**`transport.py` — `DiscogsHttp`** — the shared authenticated `requests.Session`
plus a rate-limit-aware `request()`. All direct REST calls route through it; it
retries exactly once on HTTP 429, sleeping for the server-suggested
`Retry-After` (clamped to **10s** — lowered from 30s in P-2 so a long back-off
can't park a shared executor worker; 2s default when the header is missing or
unparseable). The sleep runs on an executor thread, never the event loop. Calls
the high-level `python3-discogs-client` library makes internally
(search/release/master) are NOT routed through here — 429s there surface as
exceptions and fall through the resolver's fallback chain. Also home to `_as_id`
(write-URL ID coercion, S-5) and `_redact_url` (username masking in logs, S-4).

**`reader.py` — `DiscogsReader`** (the resolver's half) — owns the high-level
`python3-discogs-client` `Client` and the session collection index.

- **`search_collection(artist, album)` — two strategies, both matched locally
  against a session-cached collection index (P-1)**, so neither pays a
  per-candidate HTTP cost:
  - Strategy 1: search the Discogs database for up to 25 candidates and look
    each up in the local index by `release_id`; return the first owned hit.
  - Strategy 2: if strategy 1 misses, fuzzy-match the index entries on
    artist + album title locally. Catches rare/obscurely-ranked pressings.

  The index (`_get_collection_index`, built once via paginated GETs) replaced
  the old per-candidate membership GET and full re-walk.
- **`search_database`, `get_tracklist`, `get_original_year`, `_build_result`** —
  database search, tracklist (filtering Discogs "heading" pseudo-tracks),
  original-release-year from the master, and the standardised result dict.

**`writer.py` — `DiscogsCollectionWriter`** (the tracker's half) — the two writes:

- **`increment_play_count(release_id, instance_id)`** — reads the current Play
  Count, increments by 1, writes it back:
  ```
  GET  /users/{username}/collection/releases/{release_id}
       → parse current "Play Count" (default 0 if blank/unreadable; a numeric
         value is coerced, not silently skipped — B-16)
  POST /users/{username}/collection/folders/0/releases/{release_id}
       /instances/{instance_id}/fields/{field_id}
  {"value": "<current + 1>"}
  ```
  Returns `True` on HTTP 204. The POST opts into the single 429 retry because
  it's an idempotent absolute-set (B-15). The `field_id` is lazily fetched and
  cached from `/users/{username}/collection/fields`.
- **`update_last_played(release_id, instance_id)`** — POSTs today's date
  (ISO 8601 `YYYY-MM-DD`) to the Last Played field, or a graceful no-op
  returning `True` when `discogs.last_played_field_name` isn't configured.

---

### `src/metadata/coverart.py` — CoverArtFallback

Fetches cover art URLs from the MusicBrainz Cover Art Archive when a release
can't be found in Discogs. Searches up to 5 releases and returns the first
front cover thumbnail found. Returns `None` if nothing is available.

---

### `src/metadata/models.py` — Data models

**`MetadataSource`** (enum):
- `DISCOGS_COLLECTION` — found in user's personal collection
- `DISCOGS_DATABASE` — found in Discogs DB, not user's collection
- `FALLBACK` — Shazam metadata + MusicBrainz cover art

**`_SIDE_RE`** — compiled regex `r"^([A-Za-z]+)(\d+)$"` — parses Discogs
position strings like `"A1"` or `"B12"` into `(side_letter, track_number)`.

**`DisplayPalette`** — five-field dataclass carrying the current color theme:
`bg`, `surface`, `accent`, `text`, `muted` (all `(R, G, B)` tuples). It is a
passive value object; extraction lives in `src/display/palette.py`
(`extract_palette`, A-8), which quantizes the cover and **guarantees** the
muted role passes the Full-Opacity Rule (≥ 4.5:1 vs bg) by construction. Falls
back to `FALLBACK_PALETTE` when no cover art is available.

**`TracklistEntry`**: `position` (e.g. `"A1"`), `title`, `duration` (optional, e.g. `"4:37"`)

**`TrackMetadata`**: Fully resolved metadata for display and tracking.

Key fields: `title`, `artist`, `album`, `source`, `year`, `label`,
`catalog_number`, `discogs_release_id`, `discogs_instance_id`,
`cover_art_url`, `tracklist`, `genres`

`year` is the album's ORIGINAL release year, not the pressing year
(v1.4.2): `DiscogsReader._build_result` prefers
`get_original_year()` — one rate-limited GET to `/masters/{id}`, run once
per album thanks to the resolver's album cache — and falls back to
`release.year` (the pressing year) when the release has no master or the
lookup fails.  A 2026 reissue of a 2005 album displays 2005.

The positional facts below are NOT recomputed per access. They are derived once
by a `SideIndex` value object (`SideIndex.from_tracklist(tracklist, title)`,
A-5) and cached on a `cached_property`; `TrackMetadata`'s properties are thin
delegations to it. (Previously each property re-scanned the tracklist by title
on every read, which the renderer hit several times per frame.)

Key properties (all delegating to `side_index`):
- `is_last_track` — True if the current entry's POSITION matches the final
  tracklist entry's position (v1.3.4; the entry itself is located by
  normalized title).  Position matching prevents duplicate-title albums
  (reprises, title tracks) from phantom-latching `potential_last_track`
  from side A; the residual failure mode is conservative — a genuine closer
  that duplicates an earlier title is missed, never phantom-counted
- `track_display` — returns position string (e.g. `"A3"`) or `""` if not found
- `side_letter` — `"A"`, `"B"`, etc., parsed from the current entry's position
  via `_SIDE_RE`; `None` for numeric-only tracklists
- `side_position` — 1-indexed track number within the current side
- `side_total` — total tracks on the current side
- `prev_track_title` / `next_track_title` — adjacent track titles; searches
  within the current side first, then falls back to the global tracklist at side
  boundaries (e.g. B1 correctly returns A3 as its predecessor). `None` only when
  the track is the very first or very last in the full tracklist, or is not found

**`PlaySession`**: Tracks one needle-drop-to-lift session.

Key fields: `started_at`, `identified_tracks`, `potential_last_track`,
`album_release_id`, `album_instance_id`, `last_release_id` (v1.3.5 — most
recent release ID seen from any source; drives the auto-split, unlike the
latched pair which only collection-owned tracks set)

`log_track(track)` behaviour:
- Deduplicates consecutive identical tracks
- Sets `potential_last_track = True` when `track.is_last_track`
- Updates `last_release_id` from any track carrying a `discogs_release_id`
  (v1.3.5 — including DB-sourced tracks that never latch)
- Latches `album_release_id` / `album_instance_id` from the **first track that
  has BOTH IDs** — i.e. the first DISCOGS_COLLECTION-sourced track.
  DISCOGS_DATABASE results (which have a `release_id` but no `instance_id`,
  because the user doesn't own that pressing) intentionally don't latch, so
  the Discogs field-update endpoint is never called with an invalid
  `instances/None/...` URL.  FALLBACK tracks similarly don't latch.

---

### `src/state/player_state.py` — PlayerState

Central in-memory state. Single source of truth for the display and all
components that need to know what's currently playing.

**`PlayerStatus`** (enum) — four values:

| Value | Meaning |
|-------|---------|
| `IDLE` | Startup state, or after a session completes |
| `LISTENING` | Awaiting the first recognition of a fresh session |
| `PLAYING` | Track identified and displayed |
| `ERROR` | Music detected but recognition repeatedly failed (v1.4.1, "NO MATCH FOUND") |

Transitions:

```
IDLE ──(MUSIC_STARTED)──→ LISTENING ──(track committed)──→ PLAYING
  ↑                        │      ↑                            │
  │     (N straight misses)│      │(MUSIC_STARTED — needle     │
  │                        ▼      │  repositioned)             │
  │                      ERROR ───┘──(track committed)→ PLAYING│
  └──────────────(SESSION_ENDED AudioEvent / clear())─────────┘
```

When `SESSION_ENDED` fires as an `AudioEvent`, `main.py` calls `state.clear()`
which transitions directly to `IDLE` from any state.  (A
`PlayerStatus.SESSION_ENDED` enum value existed through v1.3.3 but was never
set by any code path; it was removed in v1.3.4.)  Since v1.3.4,
`MUSIC_STARTED` transitions to LISTENING **only from IDLE** (and, since
v1.4.1, from ERROR — the "REPOSITION NEEDLE TO RETRY" recovery) — during an
active session (a side flip), the status stays PLAYING so the now-playing
card remains on screen and updates in place when the next track commits.

`ERROR` is set by `RecognitionLoop._register_miss()` after
`recognition.error_after_misses` consecutive failed recognitions (default 6,
≈1 minute of unidentifiable music) **while LISTENING only** — misses during
PLAYING are routine surface noise; misses in IDLE mean nothing.  A
successful commit (`set_track`) recovers ERROR → PLAYING directly.

**Fields:** `status`, `current_track: Optional[TrackMetadata]`,
`current_raw: Optional[RawRecognitionResult]`

Observer pattern: `on_change(callback)` registers listeners; `_notify()` calls
all listeners on any state change.  A listener that raises is logged and does
not break the other listeners.  `DisplayRenderer` uses this to set its dirty
flag, queue palette transitions, and prefetch cover art.

**Notification guarantee (v1.3.3):** `set_track()` notifies on EVERY call —
including track changes while the status is already PLAYING.  (Previously it
notified only via the status transition, so every track after the first was
silently swallowed and the renderer never prefetched new cover art
mid-session.)  `set_raw()` deliberately does not notify; it stores the
pre-resolution result only.

---

### `src/display/renderer.py` — DisplayRenderer

Manages the pygame window and renders state-appropriate screens.  The run
loop sleeps at 30 fps while a palette transition is animating (for smooth
lerping) and ~10 fps otherwise (fast enough for the 1.6s pulsing-dot
animation, easier on the Pi's CPU).

**Screens:**

| State | Screen |
|-------|--------|
| `IDLE` | DirectionA empty frame: 135° diagonal-stripe cover + "NO RECORD ON PLATTER", hero "Waiting for a record" (v1.4.1; richer idle redesign still planned for v1.6.0) |
| `LISTENING` | DirectionA empty frame: ghost ring + rotating accent arc (1.4s linear), time-progressive cover label (WARMING UP → STILL LISTENING… → IDENTIFYING… M:SS), hero "Listening…" — fresh sessions only; side flips keep the card up (v1.3.4) |
| `ERROR` | DirectionA empty frame: static muted-red arc, "NO MATCH FOUND" + "REPOSITION NEEDLE TO RETRY", hero "Couldn't identify" — the stillness is the signal (boot spins; error sits) |
| `PLAYING` | Full "Museum Card" now-playing layout |

All three empty states (v1.4.1) share `_render_empty`/`_compose_empty`: the
full 1024×600 frame on the fallback palette (lerped to smoothly), status
strip with state label and state-mapped dot (boot pulses+glows accent; idle
sits static muted; error sits static red), Cover Lift shadow retained, hero
at 48px (the DESIGN.md empty-state font size exception), and ALL album
metadata suppressed — artist, album, chips, catalog footer, PREV/NEXT.
Idle and error are fully static frames, so the render loop goes quiet;
boot animates (arc + dot + ticking label).

**Now-playing layout (v1.2.1 "Museum Card" push-down + v1.4.0 fidelity):**
- Full-width header strip on a solid `surface` background: pulsing `●` dot
  with accent glow (1.6s eased opacity/scale pulse per DESIGN.md §5) +
  letter-spaced `NOW PLAYING` label (left), `SIDE A · 02 OF 03` position
  indicator (right), both JetBrains Mono
- Left panel: square album art (~440px). The download + MD5-keyed disk cache at
  `display.cover_art_cache_dir` live in `CoverArtCache` (A-15, see below); the
  renderer only loads the cached file and smoothscales it (`_load_cover`,
  in-memory `_cover_cache`). "Cover Lift" drop shadow beneath (Pillow gaussian
  blur, cached per size) and a hairline ring above
- Right panel: hero track title (Inter Tight SemiBold 72px, word-wrapped) is
  the unconstrained primary element — it claims as much vertical space as it
  naturally needs.  The accent divider line, artist name (Inter Tight Medium
  48px), album name (Newsreader italic 32px, accent color, ≤2 wrapped
  lines), and genre chips (transparent, accent @ ~33% alpha border, max 3 +
  `+N` overflow) then flow downward from the title's actual bottom edge.
  Meta footer (year · label · catalog, tracked mono muted) and the
  prev/next panel (top divider at `surface` blended 40% toward `muted`;
  PREV left / NEXT right-aligned) remain bottom-anchored regardless of
  title height.
- **Shrink-instead-of-ellipsis (v1.4.0):** hero, artist, and album all step
  their font size down when the text genuinely cannot fit (hero 4px steps,
  artist single-line, album two-line via `_fit_wrapped`).  Ellipsis appears
  in exactly one place by design: the PREV/NEXT adjacent track names.

**Dynamic color theming:**
- Palette extraction (`palette.extract_palette()`, `src/display/palette.py`, A-8:
  8-colour quantize → dominant tint → `bg`/`surface`, most vibrant → `accent`,
  near-white → `text`/`muted`, `muted` contrast-clamped to ≥ 4.5:1 vs `bg`) runs
  **off the event loop** (P-9). `_queue_palette()` — called synchronously inside
  `set_track`'s listener — only *consumes* a cached palette (targets
  `FALLBACK_PALETTE` on a miss, never decodes); `_prefetch_cover` dispatches
  `_extract_palette_async`, which decodes in an executor, caches the result, and
  re-queues. A `_wanted_cover_url` guard drops a late decode that finished after
  the displayed track already changed, so it can't repaint the current cover.
- Palettes cached per `cover_art_url` with an LRU-style cap
  (`_PALETTE_CACHE_MAX = 200`); extraction only runs once per album
- `_animated_palette()` lerps `_current_palette` → `_target_palette` over
  `_TRANSITION_SECS = 1.0` seconds; the run loop re-renders every frame during
  the transition, then drops back to ~10 fps for the pulsing-dot animation
- If a new track arrives mid-transition, `_queue_palette()` snaps
  `_current_palette` to the currently-rendered interpolated value first, so
  the new lerp starts from what the user is actually seeing
- If the computed target equals the current target — every track of the same
  album shares a cover — `_queue_palette()` returns without restarting the
  transition (v1.3.5), avoiding 1s of 30 fps rendering per track commit
  lerping a palette to itself
- `display.dynamic_theming: false` disables extraction and uses `FALLBACK_PALETTE`

**Font system (v1.4.0 — bundled fonts):** the DESIGN.md type hierarchy ships
with the app in `src/display/assets/fonts/` (all OFL-licensed): Inter Tight
SemiBold (hero), Inter Tight Medium (artist + adjacent names), Newsreader
Italic (album), JetBrains Mono (all labels).  `_font(role, size)` loads
lazily and caches per `(role, size)`; missing files fall back to the DejaVu
SysFont family so dev machines and CI without the assets still render.
Letter-spacing — which SDL_ttf doesn't support — is reproduced for mono
labels by `_render_tracked()` (per-character blits with a `tracking × size`
advance, the same arithmetic as CSS em tracking), cached in a
`_BoundedCache`.

**Performance (v1.3.3 caches + v1.4.0 static frame):** the now-playing
screen previously re-rendered every element at ~10 fps just to animate the
status dot.  The full frame — gradient, shadow, cover, ring, strip, all
text — is now composed once per (track content, palette) onto an offscreen
Surface by `_compose_now_playing()`; steady-state frames are one full-screen
blit plus the dot (`_draw_status_dot`).  During the 1s palette lerp the key
changes per frame, so composition runs at the transition cadence — same cost
profile as before, for one second per track change.  The layout is computed
once at startup (`self._layout`) instead of per frame.  Caches, all on the
shared `_BoundedCache` helper (insertion-ordered, LRU-refresh-on-get,
size-capped; unit-tested in `tests/test_renderer_caches.py`):

| Cache | Key | Cap | Saves |
|-------|-----|-----|-------|
| `_palette_cache` | cover URL | 200 | PIL quantization per album |
| `_cover_cache` | (url, w, h) | 16 | JPEG decode + smoothscale per compose |
| gradient surface | (bg, surface, w, h) | 1 | 24 full-screen circle fills per compose |
| `_label_cache` | (text, size, color, tracking) | 128 | per-character tracked-label rendering |
| `_dot_cache` | (color, glow, r, pulse-bucket) | 64 | status-dot Surface per pulse phase (P-3) |
| `_arc_rot_cache` | (radius, accent, angle-bucket) | 64 | `transform.rotate` of the boot arc per frame (P-10) |
| shadow surface | (w, h) | 1 | Pillow gaussian blur of the Cover Lift shadow |
| `_static_surface` | (track content, **`_cover_version`**, palette) | 1 | **the entire frame** at steady state |

The static-frame key carries a monotonic `_cover_version` (bumped when a cover
lands on disk) rather than `id(cover)` — object ids are recycled after GC, so the
old key could falsely match a stale frame (B-22).

The `_dirty` flag prevents redraws when nothing changed;
`DisplayRenderer._on_state_change()` is registered as a `PlayerState`
listener and sets `_dirty = True` on any change.  With
`display.reduced_motion: true` the dot renders static and the loop goes
fully quiet at steady state.  Cover-art prefetch tasks are held in a
`_bg_tasks` set (strong references) until done.

**Env vars set at import time:**
- `SDL_AUDIODRIVER=dummy` — suppresses pygame audio (display-only device)
- `DISPLAY=:0` — default X display (needed when launched via SSH or systemd)

Escape key exits the app cleanly.

---

### `src/display/cover_cache.py` — CoverArtCache

The cover-art **fetch + disk cache**, extracted from the renderer (A-15) so the
security-sensitive network boundary lives in one small, pygame-free, independently
testable place — the same split A-4 did to the Discogs God client. The renderer
holds one `CoverArtCache` and asks it for paths (`path_for` / `exists`) or
triggers a fetch (`download`, run in an executor); image decode, scaling, and
palette extraction stay in the renderer (render-loop concerns).

- **SSRF-hardened, IP-pinned download (S-1 / S-2 / S-7).** `cover_art_url` comes
  from untrusted external APIs (Discogs image `uri`, MusicBrainz Cover Art
  Archive). `download()` requires https (http upgraded for allow-listed hosts),
  restricts the host to an apex allow-list (`discogs.com`, `coverartarchive.org`,
  `archive.org`, `mzstatic.com`, exact-or-dot-boundary), and **resolves each hop's
  host exactly once**, rejecting the hop unless *every* resolved address is public
  (private/loopback/link-local/multicast/reserved/unspecified all rejected;
  IPv4-mapped IPv6 normalized first). It then **pins the connection to that one
  vetted IP** via a `urllib3.HTTPSConnectionPool` while keeping TLS SNI +
  certificate verification bound to the original hostname (`server_hostname` /
  `assert_hostname`) — closing the validate-then-resolve-again DNS-rebinding TOCTOU
  (a second, attacker-controlled DNS answer can't steer the socket to an internal
  host). Redirects are walked manually so every hop is re-validated and re-pinned;
  the body is byte-capped (10 MB) mid-stream, requires an `image/*` Content-Type,
  and is image-verified (`palette.validate_image_file`) before an atomic
  `os.replace` into the cache.
- **Disk hygiene (R-1 / R-2).** On construction it sweeps stale `.cover-*.part`
  tempfiles (orphaned by a SIGKILL between write and rename), and prunes the cache
  to a mtime-LRU bound (`max_files` / `max_bytes`) on init and after every
  successful download — the prune never evicts the file just written (guards an
  mtime-tie/coarse-SD-clock eviction race).
- **Pure:** no pygame, no palette dependency — fully unit-tested in
  `tests/test_cover_cache.py` with a mocked socket layer and a fake streamed
  response, no display required.

---

### `src/display/layouts.py` — NowPlayingLayout

All pixel geometry and font sizes for the now-playing screen. Change this
file to restyle the display without touching renderer logic.

**Layout formula (1024×600 reference geometry):**

```
header strip  = 30px tall, full width
cover art     = 440×440px square (min of sx/sy scale to stay square at any aspect ratio)
               left margin 50px, top at 60px
text panel    = starts at x=534, width=440
  track_text  = 170px tall  (defines x/y/w; .h is unused in v1.2.1 — see note)
  divider     = 2px tall, 64px wide accent line
  artist_text = 60px tall
  album_text  = 45px tall (italic)
  genre_chips = 40px tall (pill badges)
  meta_text   = 20px tall  ┐ anchored from
  prev_next   = 44px tall  ┘ bottom of content area
```

All values scale proportionally from the 1024×600 reference using `sx` and `sy`
scale factors. Cover art is forced square via `min(int(440*sx), int(440*sy))` so
it never distorts at non-16:9 resolutions. Tested at 1024×600, 800×480,
1280×720, and 640×480.

> **v1.2.1 note:** The renderer uses `track_text.x`, `track_text.y`, and
> `track_text.w` from the layout, but derives the title's maximum pixel height
> dynamically at render time: `meta_y − title_top − secondary_block_height`.
> `track_text.h` (170px) is no longer used as a fixed title slot — the title
> now expands freely up to that computed budget, and secondary elements flow
> below it. The layout rect is preserved so any future tooling reading geometry
> from the layout dataclass still has a meaningful value.

---

### `src/tracking/listen_tracker.py` — ListenTracker

Manages `PlaySession` lifecycle and triggers Discogs field updates on album
completion.

**Session lifecycle:**
1. `MUSIC_STARTED` → `_start_session()` (creates a fresh `PlaySession`)
2. Each `on_track_identified(track)` → `session.log_track(track)`.
   **Album-change auto-split (v1.3.4):** if the confirmed track's
   `discogs_release_id` differs from the session's `last_release_id`, the
   user swapped records faster than the 45s silence threshold — the current
   session is ended immediately (correctly crediting the previous record if
   its closer played) and a fresh one begins.  Detection compares against
   `last_release_id` (v1.3.5), which updates from ANY source carrying a
   release ID — comparing against the latch alone missed swaps where the
   first record was DB-resolved (never latches), letting record 2 inherit
   and be phantom-credited for record 1's completed play.  Reliable because
   the v1.3.3 album cache guarantees consistent release IDs per album;
   FALLBACK tracks (no release ID) never trigger a split
3. `SESSION_ENDED` → `_end_session()` scheduled via `asyncio.create_task`,
   with a strong reference held in `_bg_tasks` until the task completes
   (v1.3.3 — asyncio only weak-references tasks, and this one performs the
   Discogs play-count write)

**Update logic** (`_end_session()` → `_end_session_locked()` → `_finalize_session()`;
the writes use the injected `DiscogsCollectionWriter`):

```
potential_last_track == True
AND album_release_id is not None
    → writer.increment_play_count(release_id, instance_id)
    → writer.update_last_played(release_id, instance_id)   [if configured]
    → lastfm.love(last_identified_track)                   [if love_on_completion=true]

potential_last_track == True
AND album_release_id is None
    → skip Discogs (fallback metadata, not in collection)
    → lastfm.love(last_identified_track)                   [if love_on_completion=true]

potential_last_track == False
    → skip (only Side A played, or recognition never reached the last track)
```

All three updates are independent — a failure from any one is logged as a
warning and does not affect the others. The Last.fm love call runs regardless
of whether the Discogs updates succeeded or failed.

Conservative by design: no field is updated unless the full side was confirmed
played through to completion.

---

### `src/tracking/lastfm_client.py` — LastFmClient

Wraps `pylast` to scrobble tracks and mark tracks as Loved on Last.fm.
Synchronous (pylast is synchronous); async callers use `run_in_executor`,
matching the Discogs reader/writer pattern.

**Design principles:**
- Graceful no-op when not configured, when credentials are incomplete, or when
  `pylast` is not installed — no exception ever propagates out of this module
- `pylast` is imported lazily inside the constructor (not at module level) so the
  rest of the app can import cleanly even without pylast installed
- All failures are caught internally and returned as `False`

**`enabled` property:** `True` only when `self._network` was successfully
initialised (i.e. all credentials present and `pylast.LastFMNetwork(...)` did
not raise).

**`scrobble(track, timestamp)`:**
Called from `TrackCommitService.commit` for every confirmed track.

```python
self._network.scrobble(
    artist=track.artist,
    title=track.title,
    timestamp=timestamp,          # Unix timestamp recorded before resolve()
    album=track.album or None,    # empty string → None (pylast requirement)
)
```

Returns `True` on success, `False` on any exception. Returns `True` immediately
(no-op) if `enabled` is `False`.

**`love(track)`:**
Called from `ListenTracker._finalize_session()` when `potential_last_track` is
`True` and `love_on_completion` is enabled in config.

```python
self._network.get_track(track.artist, track.title).love()
```

Returns `True` on success, `False` on any exception. Returns `True` immediately
(no-op) if `enabled` is `False` or `love_on_completion` is `False`.

**Config keys:** `lastfm.scrobble_enabled`, `lastfm.api_key`, `lastfm.api_secret`,
`lastfm.session_key`, `lastfm.love_on_completion`

---

## Key data flows

### Needle drop → track on screen

```
1. AudioCapture.run()        InputStream streams continuously;
                             ChunkAssembler emits a 15s window every 10s
2. SilenceDetector.process() RMS >= threshold → emit MUSIC_STARTED
3. main.py handler           PlayerState.set_status(LISTENING) [from IDLE or ERROR]
                             ListenTracker._start_session()
4. RecognitionLoop.run()     dequeues chunk → ShazamIOBackend.recognize()
                             → RawRecognitionResult (or None)
5. _handle_result()          confirmation_required=2: need 2 matches
                             second match → on_confirmed (= TrackCommitService.commit)
                             (None while LISTENING: _register_miss();
                             error_after_misses straight misses → ERROR)
6. MetadataResolver.resolve()  album cache miss → step 1: Discogs collection
                               hit → TrackMetadata (DISCOGS_COLLECTION);
                               result cached for the album's remaining tracks
7. PlayerState.set_track()   status → PLAYING, _notify() (fires on every
                             track change, not just status changes) →
                             _dirty=True + palette queued + cover prefetched
8. DisplayRenderer._render() cover art downloaded/cached, layout rendered;
                             scaled cover + gradient served from cache on
                             subsequent frames
```

### Side plays through → Discogs + Last.fm updated

```
1. TrackCommitService.commit last track confirmed + committed
                             → LastFmClient.scrobble(track, timestamp) [in executor]
2. ListenTracker             session.log_track() → potential_last_track=True
                             album_release_id latched from first Discogs track
3. Needle lifts              silence begins
4. SilenceDetector           after 45s → SESSION_ENDED
5. ListenTracker._finalize_session()  potential_last_track + release_id present
                                 → writer.increment_play_count()
                                 → writer.update_last_played()          [if configured]
                                 → LastFmClient.love(last_track)        [if love_on_completion=true]
6. DiscogsCollectionWriter   Play Count: GET current value, increment, POST new value
                             Last Played: POST today's ISO date
                             → HTTP 204 → return True
7. LastFmClient              get_track(artist, title).love()
                             → Last.fm API → return True
8. main.py handler           PlayerState.clear() → status IDLE
9. DisplayRenderer           idle screen
```

---

## Configuration reference (`config.yaml`)

| Key | Default | Notes |
|-----|---------|-------|
| `audio.device_name` | `"USB Audio Codec"` | Substring matched against sounddevice names |
| `audio.sample_rate` | `44100` | Hz |
| `audio.chunk_seconds` | `15` | Recording window length |
| `audio.overlap_seconds` | `5` | True shared audio between consecutive chunks (must be < `chunk_seconds`; a new chunk is emitted every `chunk_seconds - overlap_seconds`) |
| `audio.silence_threshold_rms` | `0.01` | Lower = more sensitive; tune to room noise floor |
| `audio.session_end_silence_seconds` | `45` | Seconds of silence before SESSION_ENDED |
| `discogs.user_token` | — | From discogs.com → Settings → Developers |
| `discogs.username` | — | Your Discogs username |
| `discogs.play_count_field_name` | `"Play Count"` | Must match custom field name exactly (case-sensitive) |
| `discogs.last_played_field_name` | _(optional)_ | If set, writes today's date (YYYY-MM-DD) to this custom field on completion |
| `lastfm.scrobble_enabled` | `false` | Set to `true` to enable Last.fm scrobbling |
| `lastfm.api_key` | — | From last.fm/api/account/create |
| `lastfm.api_secret` | — | From last.fm/api/account/create |
| `lastfm.session_key` | — | Generated once via `python get_lastfm_session_key.py`; does not expire |
| `lastfm.love_on_completion` | `false` | If `true`, marks the last identified track as Loved on album completion |
| `display.width` / `height` | `1024` / `600` | Waveshare 7" HDMI LCD (H) native resolution |
| `display.fullscreen` | `true` | |
| `display.dynamic_theming` | `true` | Extract 5-color palette from album art via Pillow; set `false` for fixed neutral dark theme |
| `display.cover_art_cache_dir` | `"src/display/assets/cache"` | MD5-keyed JPEG cache |
| `recognition.backend` | `"shazamio"` | `"shazamio"` \| `"acrcloud"` \| `"audd"` |
| `recognition.confirmation_required` | `2` | Consecutive matching results before committing |
| `recognition.error_after_misses` | `6` | Consecutive misses while LISTENING before the ERROR screen |
| `recognition.poll_interval_seconds` | — _(required)_ | Timeout if recognition queue is empty |

---

## File map

| File | Responsibility |
|------|---------------|
| `main.py` | Entry point / composition root — wires components, runs async event loop |
| `src/config.py` | Typed config boundary — `load_config()` → `AppConfig` (frozen section dataclasses), aggregating `ConfigError` |
| `src/audio/capture.py` | Continuous USB audio streaming (InputStream), chunk dispatch, drop-oldest `_enqueue_block` |
| `src/audio/chunking.py` | Pure-numpy overlapping-window ChunkAssembler |
| `src/audio/silence.py` | RMS silence detection, AudioEvent emission (via `Signal`) |
| `src/audio/recognizer.py` | ShazamIO recognition loop + confirmation gate; emits a confirmed result via `on_confirmed` |
| `src/app/track_commit_service.py` | `TrackCommitService` — resolve → state → track → scrobble commit (B-1/B-11) |
| `src/metadata/models.py` | TrackMetadata (+ `SideIndex`), PlaySession, TracklistEntry, MetadataSource, DisplayPalette, FALLBACK_PALETTE, _SIDE_RE |
| `src/metadata/resolver.py` | 3-step metadata lookup chain (depends on `DiscogsReader`) |
| `src/metadata/errors.py` | Transient-vs-permanent external-error taxonomy (`is_transient`) |
| `src/metadata/discogs/transport.py` | `DiscogsHttp` — shared session + rate-limit-aware `request()`, `_as_id`/`_redact_url` |
| `src/metadata/discogs/reader.py` | `DiscogsReader` — collection/DB search, tracklist, original-year, result assembly |
| `src/metadata/discogs/writer.py` | `DiscogsCollectionWriter` — Play Count increment, Last Played update |
| `src/metadata/coverart.py` | MusicBrainz Cover Art Archive fallback |
| `src/state/player_state.py` | Central state, status transitions, change listeners (via `Signal`) |
| `src/util/signal.py` | `Signal[T]` — log-and-continue observer used by PlayerState + SilenceDetector |
| `src/display/layouts.py` | Pixel geometry and font sizes (restyle here) |
| `src/display/palette.py` | Cover-art palette extraction + WCAG colour science (`extract_palette`, `ensure_contrast`, `validate_image_file`) |
| `src/display/cover_cache.py` | `CoverArtCache` (A-15) — SSRF-hardened IP-pinned cover download (S-1/S-2/S-7), URL→disk cache, `.part` sweep + mtime-LRU prune (R-1/R-2); pygame-free |
| `src/display/renderer.py` | pygame window, screen rendering, palette transition, in-memory scaled-cover cache, `EmptyState` table (consumes `CoverArtCache`) |
| `src/tracking/listen_tracker.py` | PlaySession tracking, Discogs write trigger (via `DiscogsCollectionWriter`), Last.fm love call |
| `src/tracking/lastfm_client.py` | Last.fm scrobble and love — wraps pylast; graceful no-op when unconfigured |
| `get_lastfm_session_key.py` | One-time desktop auth helper — generates a Last.fm session key to paste into config.yaml |

---

## What's not yet implemented

All source modules are complete. The only remaining work requires hardware:

- **Audio capture testing** — needs the Pi + Behringer UCA222 for the live
  `sd.InputStream` integration only: the overlapping-window logic is
  unit-tested hardware-free via `tests/test_chunking.py`, and device
  matching, config guards, and constructor plumbing via `tests/test_capture.py`
  (v1.3.5, using a stubbed sounddevice module)
- **Shazam recognition testing** — needs real audio input
- **Display rendering testing** — needs the Waveshare HDMI display
- **End-to-end integration** — full needle-drop → Discogs-updated flow on hardware
- **Idle screen richness** — the v1.4.1 idle frame is the deliberate DESIGN.md
  stripe placeholder; the richer layout (last-played art grid, clock, random
  collection suggestion) is planned for v1.6.0

See `docs/testing-guide.md` for the full pre-hardware unit test suite (run
`pytest --collect-only -q | tail -1` for the current count) and
`docs/pi-setup-guide.md` for hardware bring-up instructions.
