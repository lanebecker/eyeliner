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
                                         AudioCapture (sounddevice)
                                         15s chunks, 5s overlap
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
                    ▼                   ▼                    TrackMetadata
             DisplayRenderer    DiscogsClient                         │
             (pygame, HDMI)     increment_play_count          ┌───────┴────────┐
                                update_last_played            ▼                ▼
                                LastFmClient.love()     PlayerState      ListenTracker
                                (on completion)         .current_track   .log_track()
                                                                              │
                                                                    LastFmClient.scrobble()
                                                                    (per committed track)
```

---

## Component reference

### `main.py` — Entry point

Instantiates all components, wires up event listeners, and runs the async
event loop. Key wiring:

- `SilenceDetector.on_event` → `ListenTracker.on_silence_event` + `PlayerState.set_status`
- `RecognitionLoop._commit_track` → `MetadataResolver.resolve` → `PlayerState.set_track` + `ListenTracker.on_track_identified`
- `PlayerState.on_change` → `DisplayRenderer._on_state_change` (sets dirty flag)

Registers `SIGINT`/`SIGTERM` handlers for graceful shutdown (stops capture and
display, cancels all tasks).

---

### `src/audio/capture.py` — AudioCapture

Records overlapping audio chunks from the USB interface using `sounddevice`.

**Key behaviour:**
- Finds the device by name substring match against `audio.device_name` in config
- Records `chunk_seconds` (default 15s) of mono float32 audio at 44100 Hz
- `sd.rec()` runs in a thread pool executor so it doesn't block the event loop
- After each recording, sleeps for `chunk_seconds - overlap_seconds` (default 10s)
  before starting the next, creating 5s of overlap so tracks playing across a
  chunk boundary are still captured
- Each chunk is dispatched synchronously to `SilenceDetector.process()` and
  asynchronously enqueued for `RecognitionLoop`

**Config keys:** `audio.device_name`, `audio.sample_rate`, `audio.chunk_seconds`,
`audio.overlap_seconds`

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

`ShazamIOBackend` — default backend:
- Serialises the numpy audio array to an in-memory WAV via `soundfile`
- Passes bytes to `shazamio.Shazam().recognize()`
- Extracts album from the `sections[].metadata[]` block where `title == "album"`

**Confirmation logic (`_handle_result`):**
1. If the result matches the currently playing track → skip (no re-commit)
2. If the result matches `_pending_result` → increment `_pending_count`
3. Otherwise → reset: `_pending_result = result`, `_pending_count = 1`
4. If `_pending_count >= confirmation_required` → call `_commit_track()`, reset pending state

`_commit_track()` calls:
- `state.set_raw(raw)` — stores the raw result
- Records a Unix timestamp (`int(time.time())`) before the blocking resolve
- `await resolver.resolve(raw)` — full metadata lookup
- `state.set_track(metadata)` — updates display state
- `await tracker.on_track_identified(metadata)` — logs to play session
- `lastfm.scrobble(metadata, timestamp)` — fires in an executor (non-blocking); failure is caught and logged, never interrupts the main loop

**Config keys:** `recognition.backend` (default `"shazamio"`),
`recognition.confirmation_required` (default 2),
`recognition.poll_interval_seconds` (default 30)

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

Each step runs via `asyncio.run_in_executor` (blocking API calls). Exceptions
at any step are caught and logged; execution falls through to the next step.

---

### `src/metadata/discogs_client.py` — DiscogsClient

Wraps the Discogs API for collection search and field updates.

**Libraries:**
- `python3-discogs-client` — high-level search and release fetching
- `requests.Session` — collection membership checks and custom field updates
  (endpoints the library doesn't expose cleanly)

**`search_collection(artist, album)` — two-strategy approach:**

Strategy 1 (fast): Search the Discogs database for up to 25 candidate
releases, then call the collection membership endpoint
(`/users/{username}/collection/releases/{release_id}`) for each. Returns the
first with a matching `instance_id`. Covers most common cases.

Strategy 2 (slow fallback): If strategy 1 finds nothing, page through the
user's entire collection 100 items at a time, fuzzy-matching on artist/album
title substring. Catches rare or obscurely-ranked pressings.

**`increment_play_count(release_id, instance_id)`:**

Reads the current value of the Play Count custom field, increments it by 1,
and writes it back:

```
GET  /users/{username}/collection/releases/{release_id}
     → parse current "Play Count" value (default 0 if blank or unreadable)

POST /users/{username}/collection/folders/0/releases/{release_id}
     /instances/{instance_id}/fields/{field_id}
{"value": "<current + 1>"}
```

Returns `True` on HTTP 204, `False` otherwise. The `field_id` is lazily fetched
and cached from `/users/{username}/collection/fields`. Falls back to 0 if the
GET fails or the field is blank.

**`update_last_played(release_id, instance_id)`:**

Writes today's date (ISO 8601, `YYYY-MM-DD`) to the Last Played custom field:

```
POST /users/{username}/collection/folders/0/releases/{release_id}
     /instances/{instance_id}/fields/{field_id}
{"value": "YYYY-MM-DD"}
```

Returns `True` on HTTP 204 or if `last_played_field_name` is not configured
(graceful no-op). Returns `False` on any failure. The field name is read from
`discogs.last_played_field_name` in `config.yaml`; if that key is absent, no
API calls are made.

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
`bg`, `surface`, `accent`, `text`, `muted` (all `(R, G, B)` tuples).
Extracted from album art via Pillow color quantization; falls back to
`FALLBACK_PALETTE` when no cover art is available.

**`TracklistEntry`**: `position` (e.g. `"A1"`), `title`, `duration` (optional, e.g. `"4:37"`)

**`TrackMetadata`**: Fully resolved metadata for display and tracking.

Key fields: `title`, `artist`, `album`, `source`, `year`, `label`,
`catalog_number`, `discogs_release_id`, `discogs_instance_id`,
`cover_art_url`, `tracklist`, `genres`

Key properties:
- `is_last_track` — True if `self.title` (lowercased, stripped) matches the
  final entry in `self.tracklist`
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
`album_release_id`, `album_instance_id`

`log_track(track)` behaviour:
- Deduplicates consecutive identical tracks
- Sets `potential_last_track = True` when `track.is_last_track`
- Latches `album_release_id` / `album_instance_id` from the **first**
  Discogs-sourced track (not overwritten by subsequent tracks or fallback tracks)

---

### `src/state/player_state.py` — PlayerState

Central in-memory state. Single source of truth for the display and all
components that need to know what's currently playing.

**`PlayerStatus`** (enum) — four values:

| Value | Meaning |
|-------|---------|
| `IDLE` | Startup state, or after a session completes |
| `LISTENING` | Music detected, awaiting first recognition |
| `PLAYING` | Track identified and displayed |
| `SESSION_ENDED` | Defined in the enum for renderer compatibility; not explicitly set by `main.py` in the current flow (see below) |

Transitions:

```
IDLE ──(MUSIC_STARTED)──→ LISTENING ──(track committed)──→ PLAYING
  ↑                                                            │
  └──────────────(SESSION_ENDED AudioEvent / clear())─────────┘
```

When `SESSION_ENDED` fires as an `AudioEvent`, `main.py` calls `state.clear()`
which transitions directly to `IDLE`. The `PlayerStatus.SESSION_ENDED` enum
value is not currently set on this path, but the renderer handles it explicitly
(alongside `IDLE`) so that any future code that does set it will render the
idle screen rather than a blank frame.

**Fields:** `status`, `current_track: Optional[TrackMetadata]`,
`current_raw: Optional[RawRecognitionResult]`

Observer pattern: `on_change(callback)` registers listeners; `_notify()` calls
all listeners on any state change. `DisplayRenderer` uses this to set its dirty flag.

---

### `src/display/renderer.py` — DisplayRenderer

Manages the pygame window and renders state-appropriate screens at ~30 fps.

**Screens:**

| State | Screen |
|-------|--------|
| `IDLE` / `SESSION_ENDED` | Radial gradient dark background (TODO v1.4.0: last-played art, clock) |
| `LISTENING` | Spinning arc + "IDENTIFYING…" label in muted grey |
| `PLAYING` | Full "Museum Card" now-playing layout |

**Now-playing layout (v1.2.1 "Museum Card" — dynamic push-down):**
- Full-width header strip: pulsing `●` dot + `NOW PLAYING` label (left),
  `SIDE A · 02 OF 03` position indicator (right), both in monospace
- Left panel: square album art (~440px), downloaded from URL, MD5-keyed disk
  cache at `display.cover_art_cache_dir`
- Right panel: hero track title (72px bold, word-wrapped) is the unconstrained
  primary element — it claims as much vertical space as it naturally needs.
  The accent divider line, artist name (48px), album name (32px italic, accent
  color), and genre/style chip badges (bordered pills, monospace) then flow
  downward from the title's actual bottom edge (v1.2.1 dynamic push-down).
  Meta footer (year · label · catalog, monospace muted) and prev/next track strip
  remain bottom-anchored regardless of title height. Font size reduction is a
  last resort, applied only if the title cannot fit even with the full available
  space above the secondary block.

**Dynamic color theming:**
- On each new track, `_queue_palette()` runs PIL color quantization on the
  cached cover art JPEG — 8 colors → dominant tint → `bg`/`surface`, most
  vibrant → `accent`, near-white → `text`/`muted`
- Palettes cached per `cover_art_url`; extraction only runs once per album
- `_animated_palette()` lerps `_current_palette` → `_target_palette` over
  `_TRANSITION_SECS = 1.0` seconds; the run loop re-renders every frame during
  the transition, then returns to dirty-flag mode
- `display.dynamic_theming: false` disables extraction and uses `FALLBACK_PALETTE`

**Font system:** four dicts pre-built at startup — `_fonts` (regular, with the
track-title size overwritten by a bold variant), `_italic_fonts`, `_mono_fonts`,
and `_bold_fonts` (bold title fonts at 4 px steps from the default size down to
18 px, used by the v1.2.1 font-scaling fallback) — all keyed by pixel size.
No SysFont calls during rendering.

**Performance:** `_dirty` flag prevents redraws when state hasn't changed.
`DisplayRenderer._on_state_change()` is registered as a `PlayerState` listener
and sets `_dirty = True` on any change.

**Env vars set at import time:**
- `SDL_AUDIODRIVER=dummy` — suppresses pygame audio (display-only device)
- `DISPLAY=:0` — default X display (needed when launched via SSH or systemd)

Escape key exits the app cleanly.

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
2. Each `on_track_identified(track)` → `session.log_track(track)`
3. `SESSION_ENDED` → `asyncio.create_task(_end_session())`

**`_end_session()` update logic:**

```
potential_last_track == True
AND album_release_id is not None
    → discogs.increment_play_count(release_id, instance_id)
    → discogs.update_last_played(release_id, instance_id)  [if configured]
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
matching the `DiscogsClient` pattern.

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
Called from `RecognitionLoop._commit_track()` for every confirmed track.

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
Called from `ListenTracker._end_session()` when `potential_last_track` is `True`
and `love_on_completion` is enabled in config.

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
1. AudioCapture.run()        records 15s chunk
2. SilenceDetector.process() RMS >= threshold → emit MUSIC_STARTED
3. main.py handler           PlayerState.set_status(LISTENING)
                             ListenTracker._start_session()
4. RecognitionLoop.run()     dequeues chunk → ShazamIOBackend.recognize()
                             → RawRecognitionResult (or None)
5. _handle_result()          confirmation_required=2: need 2 matches
                             second match → _commit_track()
6. MetadataResolver.resolve()  step 1: Discogs collection hit
                               → TrackMetadata (DISCOGS_COLLECTION)
7. PlayerState.set_track()   status → PLAYING, _notify() → _dirty=True
8. DisplayRenderer._render() cover art downloaded/cached, layout rendered
```

### Side plays through → Discogs + Last.fm updated

```
1. RecognitionLoop           last track identified, committed
                             → LastFmClient.scrobble(track, timestamp) [in executor]
2. ListenTracker             session.log_track() → potential_last_track=True
                             album_release_id latched from first Discogs track
3. Needle lifts              silence begins
4. SilenceDetector           after 45s → SESSION_ENDED
5. ListenTracker._end_session()  potential_last_track + release_id present
                                 → DiscogsClient.increment_play_count()
                                 → DiscogsClient.update_last_played()  [if configured]
                                 → LastFmClient.love(last_track)        [if love_on_completion=true]
6. DiscogsClient             Play Count: GET current value, increment, POST new value
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
| `audio.overlap_seconds` | `5` | Overlap between consecutive chunks |
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
| `recognition.poll_interval_seconds` | `30` | Timeout if recognition queue is empty |

---

## File map

| File | Responsibility |
|------|---------------|
| `main.py` | Entry point — wires components, runs async event loop |
| `src/audio/capture.py` | USB audio recording, overlapping chunks |
| `src/audio/silence.py` | RMS silence detection, AudioEvent emission |
| `src/audio/recognizer.py` | ShazamIO recognition loop, confirmation logic |
| `src/metadata/models.py` | TrackMetadata, PlaySession, TracklistEntry, MetadataSource, DisplayPalette, FALLBACK_PALETTE, _SIDE_RE |
| `src/metadata/resolver.py` | 3-step metadata lookup chain |
| `src/metadata/discogs_client.py` | Discogs collection/DB search, genres/styles extraction, Play Count increment, Last Played update |
| `src/metadata/coverart.py` | MusicBrainz Cover Art Archive fallback |
| `src/state/player_state.py` | Central state, status transitions, change listeners |
| `src/display/layouts.py` | Pixel geometry and font sizes (restyle here) |
| `src/display/renderer.py` | pygame window, cover art cache, screen rendering |
| `src/tracking/listen_tracker.py` | PlaySession tracking, Discogs field update trigger, Last.fm love call |
| `src/tracking/lastfm_client.py` | Last.fm scrobble and love — wraps pylast; graceful no-op when unconfigured |
| `get_lastfm_session_key.py` | One-time desktop auth helper — generates a Last.fm session key to paste into config.yaml |

---

## What's not yet implemented

All source modules are complete. The only remaining work requires hardware:

- **Audio capture testing** — needs the Pi + Behringer UCA222
- **Shazam recognition testing** — needs real audio input
- **Display rendering testing** — needs the Waveshare HDMI display
- **End-to-end integration** — full needle-drop → Discogs-updated flow on hardware
- **Idle screen** — `DisplayRenderer._render_idle()` renders a blank dark screen; a
  nicer idle layout (last-played art, clock, etc.) is marked TODO in the code

See `docs/testing-guide.md` for the full pre-hardware unit test suite (208 tests)
and `docs/pi-setup-guide.md` for hardware bring-up instructions.
