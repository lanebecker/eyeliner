# Architecture — vinyl-now-playing

A Raspberry Pi 4 listens to a turntable via a USB audio interface, identifies
tracks using Shazam, looks up pressing details in Discogs, displays the
now-playing info on an HDMI screen, and increments the Play Count in your
Discogs collection when a full side plays through.

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
                                                              ▼                ▼
                                                        PlayerState      ListenTracker
                                                        .current_track   .log_track()
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
- `await resolver.resolve(raw)` — full metadata lookup
- `state.set_track(metadata)` — updates display state
- `await tracker.on_track_identified(metadata)` — logs to play session

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

**`TracklistEntry`**: `position` (e.g. `"A1"`), `title`, `duration` (optional, e.g. `"4:37"`)

**`TrackMetadata`**: Fully resolved metadata for display and tracking.

Key fields: `title`, `artist`, `album`, `source`, `year`, `label`,
`catalog_number`, `discogs_release_id`, `discogs_instance_id`,
`cover_art_url`, `tracklist`

Key properties:
- `is_last_track` — True if `self.title` (lowercased, stripped) matches the
  final entry in `self.tracklist`
- `track_display` — returns position string (e.g. `"A3"`) or `""` if not found

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

**`PlayerStatus`** (enum) and transitions:

```
IDLE ──(MUSIC_STARTED)──→ LISTENING ──(track committed)──→ PLAYING
  ↑                                                            │
  └──────────────(SESSION_ENDED / clear())────────────────────┘
```

`SESSION_ENDED` sets status back to `IDLE` via `state.clear()` (called in
`main.py`'s silence event handler).

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
| `IDLE` / `SESSION_ENDED` | Dark background (idle screen, TODO: last-played art, clock) |
| `LISTENING` | Centered "Listening…" text in muted grey |
| `PLAYING` | Full now-playing layout (cover art + text panels) |

**Now-playing layout:**
- Left panel: square album art (85% of screen height), downloaded from URL,
  MD5-keyed disk cache at `display.cover_art_cache_dir`
- Right panel text fields: artist (36px bold), album (26px), track title
  (24px, accent colour), meta line (year · label · catalog no., 16px muted),
  track position ("A1", 16px), source badge (fallback warning, bottom-right)

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

**Layout formula (1024×600 example):**

```
cover_size = height * 0.85  = 510px  (square, left-aligned)
margin     = height * 0.075 =  45px
text_x     = cover_size + margin * 2 = 600px
text_w     = width - text_x - margin = 379px
```

Scales proportionally at any resolution. Tested at 1024×600, 800×480,
1280×720, and 640×480.

---

### `src/tracking/listen_tracker.py` — ListenTracker

Manages `PlaySession` lifecycle and triggers the Discogs Play Count update.

**Session lifecycle:**
1. `MUSIC_STARTED` → `_start_session()` (creates a fresh `PlaySession`)
2. Each `on_track_identified(track)` → `session.log_track(track)`
3. `SESSION_ENDED` → `asyncio.create_task(_end_session())`

**`_end_session()` update logic:**

```
potential_last_track == True
AND album_release_id is not None
    → discogs.increment_play_count(release_id, instance_id)

potential_last_track == True
AND album_release_id is None
    → skip (fallback metadata, not in collection)

potential_last_track == False
    → skip (only Side A played, or recognition never reached the last track)
```

Conservative by design: the Play Count is only incremented when we're confident
the full side was played through to completion.

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

### Side plays through → Discogs updated

```
1. RecognitionLoop           last track identified, committed
2. ListenTracker             session.log_track() → potential_last_track=True
                             album_release_id latched from first Discogs track
3. Needle lifts              silence begins
4. SilenceDetector           after 45s → SESSION_ENDED
5. ListenTracker._end_session()  potential_last_track + release_id present
                                 → DiscogsClient.increment_play_count()
6. DiscogsClient             GET current value, increment, POST new value
                             → HTTP 204 → return True
7. main.py handler           PlayerState.clear() → status IDLE
8. DisplayRenderer           idle screen
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
| `display.width` / `height` | `1024` / `600` | Waveshare 7" HDMI LCD (H) native resolution |
| `display.fullscreen` | `true` | |
| `display.background_color` | `[10, 10, 10]` | RGB |
| `display.font_color` | `[240, 240, 240]` | RGB |
| `display.accent_color` | `[180, 140, 80]` | Track title colour |
| `display.show_source_indicator` | `true` | Subtle badge when metadata is from fallback |
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
| `src/metadata/models.py` | TrackMetadata, PlaySession, MetadataSource dataclasses |
| `src/metadata/resolver.py` | 3-step metadata lookup chain |
| `src/metadata/discogs_client.py` | Discogs collection/DB search, Play Count update |
| `src/metadata/coverart.py` | MusicBrainz Cover Art Archive fallback |
| `src/state/player_state.py` | Central state, status transitions, change listeners |
| `src/display/layouts.py` | Pixel geometry and font sizes (restyle here) |
| `src/display/renderer.py` | pygame window, cover art cache, screen rendering |
| `src/tracking/listen_tracker.py` | PlaySession tracking, Discogs Play Count update trigger |

---

## What's not yet implemented

All source modules are complete. The only remaining work requires hardware:

- **Audio capture testing** — needs the Pi + Behringer UCA222
- **Shazam recognition testing** — needs real audio input
- **Display rendering testing** — needs the Waveshare HDMI display
- **End-to-end integration** — full needle-drop → Discogs-updated flow on hardware
- **Idle screen** — `DisplayRenderer._render_idle()` renders a blank dark screen; a
  nicer idle layout (last-played art, clock, etc.) is marked TODO in the code

See `docs/testing-guide.md` for the full pre-hardware unit test suite (138 tests)
and `docs/pi-setup-guide.md` for hardware bring-up instructions.
