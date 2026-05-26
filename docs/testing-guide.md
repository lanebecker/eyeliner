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

| File | What it tests | Needs hardware? | Needs Discogs? |
|------|--------------|-----------------|----------------|
| `tests/test_models.py` | Data models (TrackMetadata, PlaySession, etc.) | No | No |
| `tests/test_silence.py` | Silence detector state machine | No | No |
| `tests/test_listen_tracker.py` | Last-track detection & Discogs update logic | No | No |
| `tests/test_discogs_client.py` | DiscogsClient Play Count & Last Played logic | No | No |
| `tests/test_resolver.py` | 3-step metadata fallback chain | No | No |
| `tests/test_recognizer.py` | Recognition loop confirmation logic | No | No |
| `tests/test_layouts.py` | Display layout geometry | No | No |
| `test_discogs_live.py` | Live Discogs API integration | No | **Yes** |

---

## Running the unit tests

### All suites at once

```bash
pytest
```

pytest.ini points `testpaths = tests`, so this automatically picks up everything
in the `tests/` directory. Expected output:

```
============================= test session starts ==============================
platform darwin -- Python 3.9.6, pytest-8.4.2, pluggy-1.6.0
collected 193 items

tests/test_discogs_client.py .....................                          [ 11%]
tests/test_layouts.py ....................................                  [ 29%]
tests/test_listen_tracker.py ....................                           [ 40%]
tests/test_models.py ............................................             [ 62%]
tests/test_recognizer.py .................                                  [ 71%]
tests/test_resolver.py ......................                               [ 83%]
tests/test_silence.py ......................                                [100%]

=============================== warnings summary ===============================
venv/lib/python3.9/site-packages/urllib3/__init__.py:35
  NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+, currently the
  'ssl' module is compiled with 'LibreSSL 2.8.3'. See: https://github.com/
  urllib3/urllib3/issues/3020

============================== 193 passed in 0.34s ==============================
```

The `NotOpenSSLWarning` is harmless — see [Common failure modes](#common-failure-modes) below.

### One suite at a time

```bash
pytest tests/test_models.py
pytest tests/test_silence.py
pytest tests/test_listen_tracker.py
pytest tests/test_discogs_client.py
pytest tests/test_resolver.py
pytest tests/test_recognizer.py
pytest tests/test_layouts.py
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
- `is_last_track` returns `True` only for the final entry in the tracklist, case-insensitively and with whitespace tolerance
- `track_display` returns the correct position string ("A3") or `""` when not found
- `log_track()` deduplicates consecutive identical tracks
- `log_track()` sets `potential_last_track = True` when the last track is logged
- `log_track()` latches `album_release_id` from the **first** Discogs-sourced track only (not overwritten by subsequent tracks)
- Fallback tracks (no `discogs_release_id`) do not latch any IDs
- `DisplayPalette` fields round-trip correctly; `FALLBACK_PALETTE` is a valid `DisplayPalette` with very dark `bg`
- `_SIDE_RE` matches standard (`"A1"`) and multi-digit (`"B12"`) positions; does not match numeric-only strings
- `genres` field defaults to `[]` and stores values correctly
- Side-awareness properties (`side_letter`, `side_position`, `side_total`, `prev_track_title`, `next_track_title`) using the Sonic Youth *Sister* tracklist (A1–A3, B1–B4): correct side grouping, 1-indexed positions, cross-side stitching at side boundaries (e.g. B1's `prev_track_title` returns A3; A3's `next_track_title` returns B1), `None` only for the globally first or last track, `None` for unknown tracks or numeric-only position strings

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

### `test_listen_tracker.py` — Last-track detection (most important)

Mocks `DiscogsClient` and drives `ListenTracker` directly. Tests every edge case
from the architecture doc.

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

### `test_discogs_client.py` — Play Count & Last Played logic

Mocks all HTTP calls and tests `DiscogsClient.increment_play_count()`,
`update_last_played()`, and the `_get_field_value()` helper in isolation.
No real Discogs account required.

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

### `test_resolver.py` — Metadata fallback chain

Injects mock `DiscogsClient` and `CoverArtFallback` into `MetadataResolver`.

Key cases:
- Collection hit → `DISCOGS_COLLECTION` source; database step never called
- Collection miss → falls through to database; `DISCOGS_DATABASE` source
- Both miss → `FALLBACK` source; MusicBrainz cover art fetched
- Exceptions in any step fall through to the next without crashing
- `NotImplementedError` (not-yet-implemented stub) falls through gracefully
- All `TrackMetadata` fields correctly populated from each source
- `genres` list passed through from Discogs result dict; defaults to `[]` if key absent; always `[]` on fallback path

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

### `test_layouts.py` — Display geometry

Tests `get_now_playing_layout()` at multiple resolutions (1024×600, 800×480, 1280×720, 640×480).
No pygame window is opened.

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
══════════════════════════════════════════════════════════
  vinyl-now-playing — Discogs Live Test
══════════════════════════════════════════════════════════

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

══════════════════════════════════════════════════════════
  4/4 tests passed
══════════════════════════════════════════════════════════
```

### Output symbols

| Symbol | Meaning |
|--------|---------|
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

- `src/audio/capture.py` — `sounddevice` recording from the UCA222
- `src/audio/recognizer.py` → `ShazamIOBackend.recognize()` — real Shazam API calls with real audio
- `src/display/renderer.py` — pygame rendering on the HDMI display
- `main.py` end-to-end — the full event loop with all components wired together

Once the hardware arrives, a `test_integration.py` covering the full needle-drop →
track-identified → display-updated → session-ended → Discogs-updated path would be
the natural next addition.
