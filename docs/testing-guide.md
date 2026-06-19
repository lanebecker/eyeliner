# Testing Guide — vinyl-now-playing

This guide walks through every test suite in the project: what each one covers,
how to run it, and how to read the output. All tests except `test_discogs_live.py`
require zero hardware — no Raspberry Pi, no audio interface, no display.

---

## Prerequisites

### System dependency

`sounddevice` (used in production code imported by the tests) requires PortAudio
at the system level. Install it once:

```bash
# macOS
brew install portaudio

# Ubuntu / Debian / Raspberry Pi OS
sudo apt install libportaudio2
```

### Python environment

From the repo root:

```bash
git clone https://github.com/lanebecker/vinyl-now-playing.git
cd vinyl-now-playing

python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

Your prompt should now show `(venv)`. All commands below assume this environment
is active.

### Discogs credentials (only for `test_discogs_live.py`)

```bash
cp config.example.yaml config.yaml
# Edit config.yaml — fill in discogs.user_token and discogs.username
```

**Getting your token:** Discogs → Settings → [Developers](https://www.discogs.com/settings/developers)
→ Generate new token.

---

## Test inventory

All suites under `tests/` are hardware-free and Discogs-free (HTTP is mocked).
The table groups related files; for the live, authoritative file list run
`ls tests/test_*.py`.

| File(s) | What it tests |
|---------|---------------|
| `test_config.py` | Typed config parsing/validation — `AppConfig`, aggregating `ConfigError`, coercion |
| `test_models.py`, `test_tracklist_neighbours.py` | TrackMetadata + `SideIndex` (prev/next, reprise B-5, numbered B-10), PlaySession |
| `test_silence.py`, `test_silence_liveness.py` | Silence detector state machine + the B-6 liveness tick |
| `test_signal.py` | `Signal[T]` log-and-continue observer |
| `test_chunking.py` | ChunkAssembler overlapping-window logic |
| `test_capture.py` | AudioCapture device match, config guards, drop-oldest `_enqueue_block`, `stop()` (stubbed sounddevice) |
| `test_player_state.py` | PlayerState transitions & change notifications |
| `test_recognizer.py`, `_encode.py`, `_parse.py`, `_epoch.py`, `_progress.py` | Confirmation gate, `run()` loop, the encode/`_call_shazam`/`_parse` split, B-1 epoch + B-7 progress |
| `test_track_commit_service.py` | `TrackCommitService.commit` — B-1 epoch guard, B-11 ordering, Last.fm scrobble branch (T-2) |
| `test_resolver.py`, `test_resolver_error_no_cache.py` | 3-step fallback + album cache + transient-vs-permanent handling |
| `test_metadata_errors.py` | Transient/permanent error taxonomy (`is_transient`) |
| `test_discogs_reader.py` | Reader read surface — `get_tracklist` heading/positionless filter, fail-soft |
| `test_discogs_search_errors.py` | `search_collection` strategy-1→2 fallthrough + B-4/B-13 error semantics |
| `test_discogs_collection_index.py` | P-1 session collection index |
| `test_discogs_client.py`, `_robustness.py` | `DiscogsCollectionWriter` Play Count / Last Played + 429 retry (B-15) + numeric coercion (B-16) |
| `test_discogs_security.py` | Write-URL ID coercion (S-5) + URL redaction (S-4) |
| `test_discogs_split.py` | A-4 concern boundary (reader/writer share one transport) |
| `test_listen_tracker.py`, `_idempotency.py`, `_split_race.py` | Last-track detection, Discogs writes, idempotency, album-change split |
| `test_lastfm_client.py` | LastFmClient scrobble & love logic |
| `test_layouts.py` | Display layout geometry |
| `test_renderer_palette.py`, `_caches.py`, `_typography.py`, `_perf.py`, `_robustness.py` | Palette transitions incl. off-loop extraction + `_cover_version` recompose (P-9/B-22), bounded caches, typography, hot-loop perf, degenerate covers, boot-arc bucket cache (P-10) |
| `test_cover_cache.py` | `CoverArtCache` — SSRF IP-pinning + DNS-rebinding/mixed-IP rejection (S-1/S-2/S-7), `.part` sweep + mtime-LRU disk prune (R-1/R-2) |
| `test_error_state.py` | `EmptyState` rendering, miss counting, boot label |
| `test_main_wiring.py` | `main.py` pipeline wiring + shutdown semantics |
| `test_session_log_track_dedup.py` | PlaySession track-dedup logging |
| `test_discogs_live.py` (repo root, `check_*` fns) | **Live** Discogs API — manual only, **needs Discogs creds**, excluded from pytest collection (see [T-7] / `conftest.py`) |

---

## Running the unit tests

### All suites at once

```bash
pytest
```

pytest.ini points `testpaths = tests`, so this automatically picks up everything
in the `tests/` directory and runs it in well under a second (no I/O, no
hardware). The whole suite should report `… passed` with no failures.

The exact test count is **deliberately not pinned in docs** — it drifts every
time a suite is added (T-8). For the current number, run:

```bash
pytest --collect-only -q | tail -1
```

You may see a harmless `NotOpenSSLWarning` from urllib3 on macOS/LibreSSL — see
[Common failure modes](#common-failure-modes) below.

### One suite at a time

```bash
pytest tests/test_models.py
pytest tests/test_silence.py
pytest tests/test_chunking.py
pytest tests/test_capture.py
pytest tests/test_player_state.py
pytest tests/test_listen_tracker.py
pytest tests/test_discogs_client.py
pytest tests/test_lastfm_client.py
pytest tests/test_resolver.py
pytest tests/test_recognizer.py
pytest tests/test_layouts.py
pytest tests/test_renderer_caches.py
pytest tests/test_renderer_palette.py
pytest tests/test_renderer_typography.py
pytest tests/test_error_state.py
```

### With verbose output (see each test name)

```bash
pytest -v
```

### Stop on first failure

```bash
pytest -x
```

### Run a single test by name

```bash
pytest tests/test_listen_tracker.py::test_only_side_a_played_does_not_increment
```

---

## What each suite covers

### `test_models.py` — Data models

Tests `TracklistEntry`, `TrackMetadata`, `PlaySession`, `MetadataSource`,
`DisplayPalette`, `FALLBACK_PALETTE`, and `_SIDE_RE` in isolation.

Key cases:
- `is_last_track` returns `True` only for the album's final track (position-matched since v1.3.4; the entry is located by title case-insensitively and with whitespace tolerance)
- `track_display` returns the correct position string ("A3") or `""` when not found
- `log_track()` deduplicates consecutive identical tracks
- `log_track()` sets `potential_last_track = True` when the last track is logged
- `log_track()` latches `album_release_id` from the **first** Discogs-sourced track only (not overwritten by subsequent tracks)
- Fallback tracks (no `discogs_release_id`) do not latch any IDs
- `DisplayPalette` fields round-trip correctly; `FALLBACK_PALETTE` is a valid `DisplayPalette` with very dark `bg`
- `_SIDE_RE` matches standard (`"A1"`) and multi-digit (`"B12"`) positions; does not match numeric-only strings
- `genres` field defaults to `[]` and stores values correctly
- Side-awareness properties (`side_letter`, `side_position`, `side_total`, `prev_track_title`, `next_track_title`) using the Sonic Youth *Sister* tracklist (A1–A3, B1–B4): correct side grouping, 1-indexed positions, cross-side stitching at side boundaries (e.g. B1's `prev_track_title` returns A3; A3's `next_track_title` returns B1), `None` only for the globally first or last track, `None` for unknown tracks or numeric-only position strings

Key cases — position-based `is_last_track` (new in v1.3.4):
- **Regression:** a side-A track sharing the closer's title does NOT set
  last-track (pre-v1.3.4 it did, enabling phantom play counts)
- Genuine closers with unique titles still return `True`
- The current entry is still located with case/whitespace normalization
- Unknown titles return `False`

Key cases — `PlaySession.last_release_id` (new in v1.3.5):
- Set by DB-sourced tracks that never latch the `album_*` pair
- Follows the MOST RECENT release seen (the latch keeps the first)
- Unchanged by FALLBACK tracks (no release ID)

### `test_silence.py` — Silence detection

Synthesizes audio as numpy arrays (music = scaled white noise, silence = zeros) and
drives `SilenceDetector.process()` directly. Uses `unittest.mock.patch` to control
`time.monotonic` so SESSION_ENDED timing tests run instantly rather than waiting 45
real seconds.

Key cases:
- `MUSIC_STARTED` fires exactly once at the start of music
- `MUSIC_STOPPED` fires on the first silent chunk after music
- `SESSION_ENDED` fires only after silence >= `session_end_silence_seconds`
- `SESSION_ENDED` fires exactly once per session (not repeatedly)
- Silence at startup (before any music) never triggers `SESSION_ENDED`
- After `SESSION_ENDED`, new music restarts the cycle and a second `SESSION_ENDED`
  can fire for the next session
- All registered listeners receive every event

### `test_chunking.py` — Overlapping-window capture logic (new in v1.3.3)

Drives `ChunkAssembler` (the windowing engine behind `AudioCapture`) with
synthesized numpy ramps so every frame position is checkable. No sounddevice,
no hardware.

Key cases:
- Constructor validation: `chunk_frames > 0`, `1 <= hop_frames <= chunk_frames`
- No chunk emitted until a full window has accumulated
- Consecutive chunks start exactly `hop_frames` apart and genuinely share
  `(chunk - hop)` frames of identical audio
- One oversized block can emit multiple chunks in order
- Feeding 1-frame blocks produces byte-identical windows to one big feed —
  no audio is ever lost at block boundaries
- Emitted chunks are independent copies (mutating one can't corrupt later audio)
- `buffered_frames` / `reset()` housekeeping; 2-D blocks are flattened
- Integral-frame validation (v1.3.5): fractional frame counts raise a clear
  ValueError; whole-valued floats (seconds × sample_rate arithmetic) are
  coerced to int

### `test_capture.py` — AudioCapture device matching & guards (new in v1.3.5)

First-ever coverage for `capture.py`. The module imports `sounddevice` at the
top level (which needs PortAudio at import time), so the suite plants a stub
into `sys.modules["sounddevice"]` before importing — and every test patches
`src.audio.capture.sd` explicitly, so the real module is never exercised even
on machines where it is installed.

Key cases:
- Constructor reads the audio config; `overlap_seconds` defaults to 5
- `overlap >= chunk` is disabled at startup (the v1.3.3 guard)
- `_find_device_index`: case-insensitive substring matching, output-only
  devices skipped despite name matches, multi-match uses the first and warns
  with all candidates, not-found raises a ValueError naming the missing
  device and listing available inputs

Deliberately NOT covered (genuinely hardware-bound): the live `sd.InputStream`
integration — callback timing and PortAudio behavior still need the Pi.

### `test_player_state.py` — State transitions & notifications (new in v1.3.3)

First-ever coverage for `PlayerState` — added alongside the v1.3.3 fix for
the swallowed track-change notification, which this suite would have caught.

Key cases:
- Initial state is IDLE with no track/raw
- `set_status` notifies on change only; quiet when status is unchanged
- `set_track` transitions to PLAYING and notifies
- **Regression:** `set_track` notifies on every track change, including when
  the status is already PLAYING (the v1.3.3 bug)
- `set_track` notifies exactly once per call (no double-notify on the first track)
- `set_raw` stores without notifying
- `clear()` resets track + raw and notifies via the IDLE transition
- A listener that raises doesn't break other listeners

### `test_listen_tracker.py` — Last-track detection (most important)

Mocks a `DiscogsCollectionWriter` and drives `ListenTracker` directly (including
the public `on_silence_event(SESSION_ENDED)` → `create_task` path, T-5). Tests
every edge case from the architecture doc.

Key cases — Play Count:
- **Happy path:** last track identified + session ends → `increment_play_count` called with correct `release_id` and `instance_id`
- **Only Side A:** session ends before last track → NOT called
- **Missed recognition:** all tracks except the last identified → NOT called
- **Fallback metadata:** last track reached but `discogs_release_id = None` → NOT called
- **No tracks at all:** recognition never succeeded → NOT called
- **Spurious SESSION_ENDED:** no active session when event fires → no crash
- **Discogs API failure:** `increment_play_count` returns `False` → no crash, logged

Key cases — Last Played:
- **Configured:** `last_played_field_name` set → `update_last_played` called alongside `increment_play_count`
- **Not configured:** `last_played_field_name` is `None` → `update_last_played` never called
- **Failure:** `update_last_played` returns `False` → logs warning, no crash

Key cases — album-change auto-split (v1.3.4, detection via `last_release_id`
since v1.3.5):
- A confirmed track with a different `release_id` ends the old session and
  starts a fresh one (new latch, fresh track list)
- A finished record 1 (closer played) is still credited when the split fires
- An unfinished record 1 is NOT credited by the split
- No split on: same release, FALLBACK metadata (no release_id), or before
  any release has been seen
- **Regression (v1.3.5):** a DB-resolved record 1 (release_id but no latch)
  followed by a collection-owned record 2 splits correctly — record 2 starts
  clean instead of inheriting (and being phantom-credited for) record 1's
  `potential_last_track`; the reverse direction (collection → DB) still
  credits a finished record 1

### `test_discogs_client.py` — Play Count & Last Played logic

Mocks the shared HTTP seam (`DiscogsHttp.session.get`/`.post`) and tests the
`DiscogsCollectionWriter` writes — `increment_play_count()`,
`update_last_played()`, and the `_get_field_value()` helper — in isolation
(rate-limit and B-16 numeric-coercion cases live in
`test_discogs_client_robustness.py`). No real Discogs account required.

Key cases — increment_play_count:
- **Blank field:** Play Count field is empty → posts `"1"`
- **Existing count:** count `"5"` → posts `"6"`; count `"1"` → posts `"2"`
- **Garbage value:** non-integer string → logs a warning, treats as 0, posts `"1"`
- **Whitespace-only value:** treated as 0, posts `"1"`
- **Field not found:** `"Play Count"` absent from collection fields → returns `False`, no POST
- **GET failure:** current-value GET returns non-200 → falls back to 0, still posts `"1"`
- **POST non-204:** returns `False`
- **POST 401:** returns `False`
- **Exception during POST:** caught, returns `False`, no crash

Key cases — update_last_played:
- **Not configured:** `last_played_field_name` is `None` → returns `True`, no API calls
- **Happy path:** posts today's ISO date (YYYY-MM-DD), returns `True`
- **Date format:** posted value is always a valid parseable ISO 8601 date
- **Field not found:** `"Last Played"` absent from collection fields → returns `False`, no POST
- **POST non-204 / 401:** returns `False`
- **Exception during POST:** caught, returns `False`, no crash

Key cases — _get_field_value:
- **Correct instance:** returns the value string
- **Wrong instance_id:** returns `None`
- **Non-200 GET:** returns `None`
- **Field not in notes:** returns `None`

Key cases — `DiscogsHttp.request` rate-limit handling (`time.sleep` is patched,
so none of these actually wait):
- **429 then success:** retried once, sleeping for the `Retry-After` value
- **Missing/unparseable `Retry-After`:** falls back to the 2s default
- **Oversized `Retry-After`:** clamped to the 10s cap (lowered from 30s in P-2)
- **Success:** no retry, no sleep
- **Two consecutive 429s:** second response returned as-is (no infinite retry)
- **POST routing:** dispatches via `session.post` with default timeout applied
- **End-to-end:** `increment_play_count` succeeds through one 429 on the POST

Key cases — get_original_year / year preference (new in v1.4.2):
- **Master year preferred:** a 2026 reissue with master year 2005 → `"2005"`,
  fetched from `/masters/{id}` via the rate-limited `DiscogsHttp.request` helper
- **No master:** returns `None`, no HTTP call made
- **Master year 0:** Discogs's "unknown" sentinel → `None` (never displays "0")
- **Network failure / lazy `.master` raising:** caught, returns `None`
- **`_build_result` preference:** uses the original year when available and
  falls back to the pressing year (`release.year`) when it isn't

### `test_lastfm_client.py` — Last.fm scrobble & love logic

Mocks `pylast` at the `sys.modules` level so no real network calls are made.
No Last.fm account required.

Key cases:
- **Disabled config:** `scrobble_enabled: false` → `enabled` is `False`; `scrobble()` and `love()` return `True` (no-op)
- **Missing config section:** no `lastfm` key in config → no crash, not enabled
- **Incomplete credentials:** any of `api_key`, `api_secret`, or `session_key` absent → warns, not enabled
- **pylast not installed:** `ImportError` during import → warns, not enabled, no crash
- **scrobble happy path:** calls `network.scrobble(artist, title, timestamp, album)` with correct args, returns `True`
- **Empty album → `None`:** `track.album == ""` → `album=None` passed to pylast (not empty string)
- **scrobble exception:** pylast raises → returns `False`, does not propagate
- **love happy path:** calls `network.get_track(artist, title).love()`, returns `True`
- **love_on_completion=False:** `love()` is a no-op returning `True`; `get_track` never called
- **love exception:** pylast raises → returns `False`, does not propagate
- **enabled property:** `True` only when all credentials present and network initialised
- **love_on_completion property:** reflects `love_on_completion` from config

### `test_resolver.py` — Metadata fallback chain

Injects a mock `DiscogsReader` and `CoverArtFallback` into `MetadataResolver`.

Key cases:
- Collection hit → `DISCOGS_COLLECTION` source; database step never called
- Collection miss → falls through to database; `DISCOGS_DATABASE` source
- Both miss → `FALLBACK` source; MusicBrainz cover art fetched
- Exceptions in any step fall through to the next without crashing
- `NotImplementedError` (not-yet-implemented stub) falls through gracefully
- All `TrackMetadata` fields correctly populated from each source
- `genres` list passed through from Discogs result dict; defaults to `[]` if key absent; always `[]` on fallback path

Key cases — album-level cache (new in v1.3.3):
- Second track from the same album served from cache — Discogs called once,
  per-track fields (title) still correct, album-level fields shared
- Cache key normalizes case and whitespace
- Database-tier results cached too
- Fallback cached only when both Discogs tiers completed cleanly; a raised
  exception (network blip) leaves the album retryable
- Different albums resolve independently; cache is bounded at `_ALBUM_CACHE_MAX`

### `test_recognizer.py` — Confirmation logic

Drives `RecognitionLoop._handle_result()` with canned `RawRecognitionResult` objects.
No Shazam API calls, no audio.

Key cases:
- Single result does not commit (`confirmation_required = 2` needs two)
- Two consecutive matching results commit the track
- A different result in between resets the counter (no commit after mismatch)
- `None` (unrecognized) also resets the counter
- Same track as `current_raw` is silently skipped (no re-commit for the same song)
- `confirmation_required = 3` requires three, `= 1` commits immediately
- After a commit, pending state is cleared

Key cases — enqueue drop-oldest policy (new in v1.3.5):
- When the queue is full, the OLDEST chunk is evicted and the incoming one
  admitted (freshest audio wins; matches AudioCapture's block-queue policy)
- Below capacity, nothing is dropped

### `test_renderer_caches.py` — Renderer caches & color math (new in v1.3.3)

Tests the pure-Python pieces of `DisplayRenderer` headlessly — importing
`src.display.renderer` does not import pygame (pygame imports live inside
methods), so no display or pygame installation is exercised.

Key cases — `_BoundedCache` (backs the palette, scaled-cover, and gradient caches):
- `get()` returns `None` on miss, the stored value on hit
- Eviction drops the OLDEST entry beyond `max_entries`
- `get()` refreshes an entry's eviction position (LRU-ish)
- `put()` on an existing key replaces the value and refreshes position
- `__contains__` / `__len__`; a one-entry cache holds only the latest value

Key cases — color helpers:
- `_lerp_color` endpoints, midpoint, and clamping of `t` outside [0, 1]
- `_lerp_palette` interpolates all five palette channels
- `_clamp_luminance` brightens too-dark colors, leaves bright colors and
  pure black unchanged

### `test_renderer_palette.py` — _queue_palette decisions (new in v1.3.5)

Drives `DisplayRenderer._queue_palette` headlessly — the method never imports
pygame, and the renderer skeleton is built via `__new__` with only the
attributes the method reads (the `test_resolver.py` pattern).

Key cases:
- `dynamic_theming: false` → never retargets
- Unknown URL with no cached cover file (and `None` URL) → `FALLBACK_PALETTE`
- Palette-cache hit → cached palette becomes the target, transition starts
- **Same-target skip (v1.3.5):** re-queuing an unchanged palette does NOT
  restart the 1s transition (previously every track commit re-triggered
  30 fps rendering lerping a palette to itself)
- A genuinely different palette retargets and restarts the timer

### `test_renderer_typography.py` — Typography & fidelity helpers (new in v1.4.0)

Covers the design-translation behaviors from the v1.4.0 fidelity release.
Uses the `__new__` renderer-skeleton pattern; pygame.font is initialized
module-wide, and the compose smoke test runs under SDL's dummy video driver
(set via env vars at the top of the file), so the whole module is headless.

Key cases:
- `_wrap_lines` — short text stays on one line, long text wraps within the
  available width, empty text produces no lines
- `_fit_wrapped` (shrink-instead-of-ellipsis) — text that fits keeps the
  base size; a long album shrinks until it fits two lines; a long artist
  shrinks to a single line; the `min_size` floor is respected even for
  text that can never fit
- `_ellipsize` — short text passes through; truncated text ends with `…`
  and fits the available width (PREV/NEXT panel only)
- `_chip_texts` — ≤3 genres pass through; 5 genres collapse to 3 + `+2`;
  empty list stays empty
- `_contrast_ratio` / `_ensure_contrast` — black-on-white is 21:1; the
  fallback muted already passes against the fallback bg; failing colors are
  lightened until ≥4.5:1, including against cool-dark backgrounds (the
  DESIGN.md Cavetown case)
- **Compose smoke test:** `_compose_now_playing` renders a full 1024×600
  frame headlessly without error, and `_draw_status_dot` draws over it —
  one test that catches API drift across every drawing helper

### `test_error_state.py` — ERROR state & empty-state rendering (new in v1.4.1)

Covers the Phase 2 design-translation work: the new `PlayerStatus.ERROR`,
`RecognitionLoop` miss counting, the time-progressive boot label, and the
boot/idle/error empty-state composition.  Same headless patterns as the rest
of the suite (MagicMock-backed loop, `__new__` renderer skeleton, SDL dummy
video driver).

Key cases:
- ERROR recovers to IDLE via `clear()` and to PLAYING via `set_track()`
- `error_after_misses` consecutive misses while LISTENING surface ERROR;
  one fewer stays LISTENING
- **Misses during PLAYING never error** — surface noise and quiet passages
  must not put NO MATCH FOUND over a correctly identified record
- Misses in IDLE are ignored; the streak resets when leaving LISTENING
- A successful recognition resets the miss count (`_handle_result` path)
- Boot label progression: WARMING UP (0–19s) → STILL LISTENING… (20–59s) →
  IDENTIFYING… M:SS (60s+), boundary values pinned
- All three empty-state frames compose headlessly at full screen size
- The empty static-frame cache is reused while the boot label is unchanged
  and recomposes when the label ticks

### `test_layouts.py` — Display geometry

Tests `get_now_playing_layout()` across resolutions. No pygame window is opened.

Key cases:
- All 9 rects (`header_strip`, `cover_art`, `track_text`, `divider`, `artist_text`, `album_text`, `genre_chips`, `meta_text`, `prev_next`) have positive dimensions and non-negative coordinates
- Nothing bleeds off-screen at any tested resolution
- Cover art is square at all resolutions (including non-16:9 like 800×480)
- Header strip spans full width and starts at y=0
- Text panels start to the right of the cover art's right edge
- Vertical ordering: track → divider → artist → album → chips → meta → prev/next
- Font size hierarchy: track ≥ artist ≥ album ≥ meta/chips/header
- Chip and divider geometry is positive/non-negative
- Layout scales proportionally: larger resolution → larger cover art, wider text panels, larger fonts
- **Resolution-independence matrix (D-2, #74):** a parametrized 480×320 → 4K run
  **plus non-16:9 cases (square, portrait, ultra-wide, 5:4)** that exercise the
  `min(sx, sy)` cover branch — asserting no negative/off-screen rects, a square
  cover bound by the smaller dimension and clear of the text column, the title
  block clear of the bottom meta/prev-next, and font floors + hierarchy at every
  size. (Backs CLAUDE.md's "resolution-independent" claim for the static layout;
  the renderer's runtime title push-down is content-dependent and remains a
  hardware/visual check — see `docs/first-boot-checklist.md` §5.)

### `test_cover_cache.py` — Cover fetch + disk cache (new in v1.5.1)

Tests `CoverArtCache` (`src/display/cover_cache.py`, A-15) — the SSRF-hardened
cover download and disk hygiene, extracted from the renderer. Pygame-free; DNS
resolution and the pinned HTTPS opener are mocked, so no real network is used.

Key cases:
- **Host allow-list (S-1):** apex match is exact-or-dot-boundary —
  `i.discogs.com` allowed, `evilcoverartarchive.org` / `discogs.com.attacker.net`
  rejected
- **IP validation + DNS-rebinding (S-7):** resolves once; rejects the hop if ANY
  address is non-public (private/loopback/link-local/multicast/reserved/
  unspecified, and IPv4-mapped IPv6), rejects mixed public+private answer sets,
  normalizes a mapped public address, fails closed on resolution error
- **Pinned stream contract:** `_open_cover_stream` dials the vetted IP but sets
  `server_hostname`/`assert_hostname` to the hostname (SNI + cert), `redirect=False`
- **Download path (S-1/S-2):** rejects non-`image/*` Content-Type and HTTP ≥400,
  aborts past the byte cap mid-stream, rejects undecodable bytes, follows and
  re-pins validated redirects, and pins to the vetted IP per hop
- **Disk hygiene (R-1/R-2):** `.part` tempfiles swept on init; mtime-LRU prune by
  file count and byte budget, the just-written cover protected from eviction even
  on an mtime tie, non-`.jpg` files left untouched

---

## Running the live Discogs integration test

`test_discogs_live.py` (in the repo root, not in `tests/`) makes real network calls
to the Discogs API. It uses Sonic Youth's *Sister* as the test album.

> **Requires `config.yaml`** with a valid `user_token` and `username`.

### Read-only (safe to run any time)

```bash
python test_discogs_live.py
```

This tests:
1. `search_collection` — looks for *Sister* in your collection
2. `search_database` — looks for *Sister* in the Discogs database (all releases)
3. `get_tracklist` — fetches the full tracklist for the found release
4. Collection custom fields — verifies your "Play Count" field exists and returns its ID

### Sample output

```
════════════════════════════════════════════════════════
  vinyl-now-playing — Discogs Live Test
════════════════════════════════════════════════════════

TEST 1: search_collection("Sonic Youth", "Sister")
──────────────────────────────────────────────────
  ✓ Found in collection!
    Album:    Sister
    Year:     1987
    Label:    SST Records
    Cat #:    SST 134
    Cover:    https://img.discogs.com/...
    Release ID:  123456
    Instance ID: 789012
    Tracklist:   7 tracks

TEST 2: search_database("Sonic Youth", "Sister")
──────────────────────────────────────────────────
  ✓ Found in database (release 123456)

TEST 3: get_tracklist(release_id)
──────────────────────────────────────────────────
  ✓ 7 tracks:
    A1 · Catholic Block
    A2 · Pipeline/Kill Time
    ...

TEST 4: Collection custom fields
──────────────────────────────────────────────────
  ✓ Found "Play Count" → field ID 6

════════════════════════════════════════════════════════
  4/4 tests passed
════════════════════════════════════════════════════════
```

### Output symbols

| Symbol | Meaning |
|--------|----------|
| `✓` | Passed — got expected data |
| `✗` | Failed — API error or unexpected response |
| `·` | Skipped — e.g. *Sister* not in your collection; collection-specific tests N/A |

If *Sister* isn't in your collection, TEST 1 will show `·` and TEST 3 will use the
release ID from TEST 2 instead. That's fine — the important thing is that your token
is valid and the `Play Count` field is found.

### With the write test (modifies your Discogs collection)

```bash
python test_discogs_live.py --test-write
```

This additionally tests `increment_play_count`. It will prompt for confirmation before
making any changes. The field is a running counter — running this test will increment
the Play Count by 1 on the test release. If you want to correct it afterward, adjust
the value manually in your Discogs collection.

---

## Common failure modes

**`ModuleNotFoundError: No module named 'src'`**  
Make sure you're running from the **repo root**, not from inside `tests/`:
```bash
cd vinyl-now-playing   # repo root
pytest                 # correct
```

**`OSError: PortAudio library not found`**  
Install the system dependency: `brew install portaudio` (Mac) or `sudo apt install libportaudio2` (Linux/Pi).

**`RuntimeError: no running event loop`** in async tests  
This is fixed by `asyncio_mode = auto` in `pytest.ini`. If you see it, check that `pytest-asyncio >= 0.23` is installed: `pip install "pytest-asyncio>=0.23"`.

**`NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+`** on macOS  
Harmless. macOS ships with LibreSSL instead of OpenSSL, and urllib3 v2 emits this warning when it detects it. The tests all pass and nothing is broken — the Pi runs Linux with proper OpenSSL and will never show this warning. You can safely ignore it.

**`discogs_client.exceptions.HTTPError: 401 Unauthorized`** in live test  
Your `user_token` in `config.yaml` is wrong or expired. Regenerate it at discogs.com/settings/developers.

**`KeyError: 'Play Count'`** in live test  
The `play_count_field_name` in `config.yaml` doesn't exactly match your Discogs custom field name. It's case-sensitive — check the spelling in your Discogs collection settings.

---

## What's not tested yet (requires hardware)

These components need the actual Pi + USB audio interface + display to test:

- `src/audio/capture.py` — only the live `sd.InputStream` integration with
  the UCA222 now: the overlapping-window logic is covered by
  `tests/test_chunking.py`, and device matching / config guards by
  `tests/test_capture.py` (stubbed sounddevice)
- `src/audio/recognizer.py` → `ShazamIOBackend.recognize()` — real Shazam API calls with real audio
- `src/display/renderer.py` — pygame rendering on the HDMI display (the
  bounded caches and palette color math are covered hardware-free by
  `tests/test_renderer_caches.py`)
- `main.py` end-to-end — the full event loop with all components wired together

Once the hardware arrives, a `test_integration.py` covering the full needle-drop →
track-identified → display-updated → session-ended → Discogs-updated path would be
the natural next addition.

For the manual verification + tuning to run the first time the assembled unit
powers on — audio-device match, `silence_threshold_rms` tuning, the live cover
fetch / S-7 IP-pinned-TLS smoke test, the recognition churn breadcrumb, the
runtime title push-down, and full-pipeline + autostart checks — see
**`docs/first-boot-checklist.md`**.
