# Changelog

All notable changes to vinyl-now-playing are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versions follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

---

## [Unreleased]

_Nothing yet. Add notes here as features are built, then move them under a
new version heading when VERSION is bumped._

---

## [1.3.3] — 2026-06-10

**Bug-fix and performance release — no new features.** A full-codebase deep
review found one real notification bug, one capture-pipeline design flaw
masquerading as a feature, a Discogs API usage pattern flirting with the rate
limit, two render-loop hot paths doing per-frame work that should have been
cached, and a handful of asyncio hygiene issues. All fixed in one pass.
Test count: 210 → 261 (three new test files plus additions to three existing
ones), including the first-ever tests for `PlayerState`.

### Fixed

- **`PlayerState.set_track()` swallowed every track change after the first**
  (`src/state/player_state.py`).  `set_track()` notified listeners only via
  `set_status(PLAYING)`, which no-ops when the status is already PLAYING —
  so for track 2 onward, `DisplayRenderer._on_state_change()` never fired,
  meaning no cover-art prefetch and no palette transition for any track whose
  cover URL differed from the previous one (fallback-sourced tracks, or
  changing records without a 45s silence gap).  `set_track()` now notifies
  exactly once on every call.  Caught by the new `test_player_state.py` —
  `PlayerState` previously had zero test coverage.

- **"Overlapping" capture chunks actually had a dead gap between them**
  (`src/audio/capture.py`, new `src/audio/chunking.py`).  The capture loop
  recorded a 15s chunk with blocking `sd.rec()`, then slept for
  `chunk_seconds - overlap_seconds` (10s) during which nothing was recorded —
  the documented 5s overlap was in reality a 10s blind spot, delaying
  music/silence transition detection by up to ~25s.  Capture now records
  continuously via `sd.InputStream`; a new pure-numpy `ChunkAssembler` emits
  a 15s window every 10s with a genuine 5s shared region between consecutive
  chunks.  No audio is ever dropped between windows.  Fully unit-tested
  without hardware (`tests/test_chunking.py`).

- **Ctrl+C produced a RuntimeError traceback on every shutdown** (`main.py`).
  The old `shutdown()` cancelled ALL tasks (including `main()` itself) and
  then called `loop.stop()` from inside `asyncio.run()`, which guarantees
  `RuntimeError: Event loop stopped before Future completed`.  Signal
  handlers now simply cancel the gathered pipeline tasks; `main()` unwinds
  through a `finally` block that stops capture and display, and
  `asyncio.run()` exits cleanly.

- **Fire-and-forget `asyncio.create_task()` results were never referenced**
  (`src/tracking/listen_tracker.py`, `src/display/renderer.py`).  asyncio
  holds only weak references to tasks, so a running task can in principle be
  garbage-collected mid-flight — and one of these tasks performs the Discogs
  play-count write.  Both classes now hold strong references in a
  `_bg_tasks` set, discarded via done-callback.

### Changed

- **Album-level metadata cache in `MetadataResolver`**
  (`src/metadata/resolver.py`).  A single Discogs resolve can cost 30+ HTTP
  requests (database search, up to 25 collection-membership checks, release
  + tracklist fetches), and every track on an album repeats the identical
  (artist, album) lookup.  `resolve()` now caches results per normalized
  (artist, album) key — Discogs hits and clean fallbacks alike — cutting
  per-LP API traffic by roughly 90%.  Fallback results are cached only when
  both Discogs tiers completed without raising, so a transient network error
  never pins an album to fallback metadata.  Bounded at 64 albums with
  LRU-style eviction.

- **Discogs 429 rate-limit handling** (`src/metadata/discogs_client.py`).
  All direct REST calls now route through a `_request()` helper that retries
  exactly once on HTTP 429, honoring the server's `Retry-After` header
  (clamped to 30s, defaulting to 2s when absent or unparseable).  Discogs
  allows 60 requests/minute; previously a 429 simply failed the operation.

- **Scaled cover art is now cached** (`src/display/renderer.py`).  The
  render loop re-renders ~10×/second to animate the pulsing dot, and every
  frame re-loaded the cover JPEG from disk and re-`smoothscale`d it — the
  single largest constant CPU cost on the Pi.  `_load_cover()` now caches
  the scaled Surface keyed by (url, w, h) in a 16-entry bounded cache.

- **Gradient background is now cached** (`src/display/renderer.py`).  The
  radial gradient (24 full-screen circle fills) is rendered once per
  (palette, size) onto an offscreen Surface and re-blitted each frame.  It
  only regenerates while a palette transition is actively lerping.  Together
  with the cover cache, steady-state render CPU drops by roughly an order of
  magnitude.

- **Shazam client is now reused across recognitions**
  (`src/audio/recognizer.py`).  `ShazamIOBackend` previously constructed a
  fresh `Shazam()` object (and its internal HTTP machinery) for every chunk,
  several times a minute.  One client is now created lazily on first use and
  reused.

- **`_BoundedCache` extracted as a reusable helper**
  (`src/display/renderer.py`).  The palette, scaled-cover, and gradient
  caches all share one insertion-ordered, LRU-refresh-on-get, size-capped
  implementation (previously inline dict juggling for the palette cache
  only).  Pure Python and unit-tested in `tests/test_renderer_caches.py`.

- **`overlap_seconds >= chunk_seconds` is now rejected at startup**
  (`src/audio/capture.py`).  Previously this misconfiguration was silently
  clamped to a zero-second sleep; it now logs a clear warning and disables
  overlap (the old clamp produced an infinite re-recognition of the same
  audio under the new windowing).

### Added

- **`tests/test_player_state.py`** (9 tests) — first coverage for
  `PlayerState`, including the regression test for the set_track
  notification bug and listener-exception isolation.
- **`tests/test_chunking.py`** (13 tests) — pins the ChunkAssembler
  windowing contract: overlap correctness, no lost audio across block
  boundaries, emitted chunks are independent copies, validation.
- **`tests/test_renderer_caches.py`** (13 tests) — `_BoundedCache` semantics
  (eviction order, LRU refresh, replacement) and the palette color math
  (`_lerp_color`, `_lerp_palette`, `_clamp_luminance`).
- **`tests/test_resolver.py`** (+7 tests) — album-cache behavior: cache hits
  skip Discogs, key normalization, fallback-caching rules, transient-error
  retry, bounded eviction.
- **`tests/test_discogs_client.py`** (+8 tests) — `_request()` rate-limit
  behavior: Retry-After honored/defaulted/capped, single retry only, POST
  routing, end-to-end increment-survives-429.
- **`tests/test_listen_tracker.py`** (+1 test) — `_end_session` task is
  strongly referenced until completion.

---

## [1.3.2] — 2026-05-26

**Bug-fix release — no new features.** Follow-up QA sweep of the v1.3.1
codebase identified four real bugs (including one site the v1.3.1
async-loop migration missed), four documentation inaccuracies, and nine
smaller hardening opportunities. Everything was fixed in a single pass.
Test count: 208 → 210 (two new model-level regression tests).

### Fixed

- **`resolver.py` was missed by the v1.3.1 `get_event_loop()` sweep**
  (`src/metadata/resolver.py`). The v1.3.1 CHANGELOG enumerated four files
  it swept; `resolver.py` was an eighth site that should have been included
  and wasn't.  `MetadataResolver.resolve()` (line 35) still called
  `asyncio.get_event_loop()` from inside a coroutine. Replaced with
  `asyncio.get_running_loop()` to match the rest of the codebase.

- **Dirty-flag clobber froze the pulsing dot and identifying spinner**
  (`src/display/renderer.py`).  The v1.3.1 fix that set
  `self._dirty = True` at the end of `_render_now_playing()` /
  `_render_listening()` was immediately overwritten by `self._dirty = False`
  one line later in the run loop, so the animation only ran during the 1s
  palette transition and then froze.  Reset `_dirty` BEFORE calling
  `_render()`, so the inner code can re-dirty for the next frame.

- **`PlaySession.log_track` latched a release_id without an instance_id**
  (`src/metadata/models.py`).  `DISCOGS_DATABASE` results legitimately have
  `discogs_release_id` set but `discogs_instance_id = None` (the user
  doesn't own that pressing).  The old guard `if release_id is None and
  track.discogs_release_id:` accepted these, which meant `_end_session()`
  later called `increment_play_count(release_id, None)` — building a
  URL ending in `…/instances/None/fields/…` that Discogs guaranteed to
  reject.  Tightened the guard to require BOTH IDs before latching.

- **Misleading test name + assertion in `test_listen_tracker.py`** —
  `test_database_source_without_instance_id_does_not_increment` was named
  as if it asserted the call was suppressed, but the body asserted
  `assert_called_once_with(12345, None)`, documenting the bug instead of
  catching it.  Renamed to
  `test_database_source_without_instance_id_does_not_call_increment` and
  flipped the assertion to `assert_not_called()` for both
  `increment_play_count` and `update_last_played`.

- **Inaccurate `CLAUDE.md` config snippet** — listed `discogs.token` but
  the actual key (used by `README.md`, `config.example.yaml`,
  `docs/architecture.md`, `docs/pi-setup-guide.md`, and the code) is
  `discogs.user_token`.  Corrected and expanded the snippet to include
  the other commonly-needed keys (`play_count_field_name`,
  `scrobble_enabled`, etc.) for accuracy.

- **`CLAUDE.md` `PlaySession` description was out of date** — described
  latching as "first Discogs-sourced track only," which under the new
  tightened rule is misleading.  Updated to spell out that BOTH IDs are
  required to latch, so DB-only results don't pre-empt the slot.

- **Architecture diagram in `docs/architecture.md`** showed
  `LastFmClient.love()` dangling under DisplayRenderer.  Re-grouped it
  under ListenTracker, where it actually runs.

### Added

- **HTTP timeouts on every Discogs API call** (`src/metadata/discogs_client.py`).
  Every `self._session.get` and `self._session.post` now passes
  `timeout=15`.  The high-level `discogs_client.Client` (used for
  `search()` and `release()` calls) gets matching limits via
  `set_timeout(connect=5, read=15)`.  Previously, a flaky CDN connection or
  a hung TCP socket could occupy an executor thread for minutes before the
  OS-level timeout kicked in.

- **Atomic, timeout-aware cover-art download**
  (`src/display/renderer.py`).  Replaced `urllib.request.urlretrieve` with
  a `requests.get(..., timeout=15, stream=True)` flow that writes to a
  `tempfile.NamedTemporaryFile` in the cache directory and then
  `os.replace`s into the final path.  No more half-written cache files
  surviving a network drop or process kill; no more unbounded executor
  thread occupancy.

- **Improved audio-device matching diagnostics**
  (`src/audio/capture.py`).  `_find_device_index` now logs all matching
  candidates when more than one input device matches the configured
  `device_name`, so users with multiple USB audio devices (e.g. UCA222 +
  USB mic) can see which one was picked from the logs.

- **Case- and whitespace-insensitive track comparison**
  (`src/audio/recognizer.py`).  Added a `_same_track` helper that
  `.strip().lower()`s title and artist before comparing.  Shazam
  occasionally returns subtly different formatting for the same track
  between chunks; without normalization those count as a new track and
  trigger an unnecessary re-resolve / re-scrobble.

- **Debug log when a recognition chunk is dropped**
  (`src/audio/recognizer.py`).  `enqueue` used to silently drop chunks
  when the queue was full; now it logs at DEBUG level so a "stopped
  identifying tracks" complaint has a breadcrumb in the journal.

- **Bounded `_palette_cache`** (`src/display/renderer.py`).  Added a
  200-entry LRU-ish cap so the per-cover palette cache can't grow
  unbounded over very long uptimes.  Re-running extraction on a cache
  miss is cheap (~ms per album), so eviction is harmless.

- **Mid-transition palette snap** (`src/display/renderer.py`).  If a new
  track arrives before the previous 1s palette lerp completes,
  `_queue_palette` now snaps `_current_palette` to the currently-rendered
  interpolated value before reassigning the target — so the new lerp
  starts from what the user is *currently seeing* instead of from a
  stale base palette.

- **Adaptive render cadence** (`src/display/renderer.py`).  The run loop
  now sleeps `1/30s` only during a palette transition (smooth lerp); the
  rest of the time it sleeps `1/10s`, plenty for the 0.8s pulsing dot
  but easier on the Pi's CPU.

- **README `venv` step** — added the standard `python3 -m venv venv` +
  activate instructions to the Setup block, matching what
  `docs/pi-setup-guide.md` already recommended.

- **Hardened `sync-version-badge.yml` regex** — replaced `[^-]*` with a
  pattern that survives hyphenated pre-release versions like `1.4.0-rc1`.

- **Two new regression tests in `tests/test_models.py`** covering the
  PlaySession latching tightening:
  - `test_log_track_does_not_latch_database_source_without_instance_id`
  - `test_log_track_database_then_collection_latches_collection_only`

---

## [1.3.1] — 2026-05-25

### Fixed

- **`asyncio.get_event_loop()` deprecated calls** — seven calls to the
  deprecated `asyncio.get_event_loop()` inside coroutines were replaced with
  `asyncio.get_running_loop()` across four files. `get_event_loop()` emits a
  `DeprecationWarning` in Python 3.10+ and raises `RuntimeError` in some
  contexts; `get_running_loop()` is the correct API inside a running event loop
  and raises `RuntimeError` immediately if called outside one, making bugs
  easier to catch.
  - `src/audio/capture.py` — `run()` coroutine (×1)
  - `src/audio/recognizer.py` — `_commit_track()` coroutine (×2, executor call
    for Last.fm scrobble)
  - `src/tracking/listen_tracker.py` — `_end_session()` coroutine (×3, all
    three `run_in_executor` calls for Discogs and Last.fm)
  - `main.py` — `shutdown()` coroutine (×1, `loop.stop()`)

- **ShazamIO album extraction nested-loop break** (`src/audio/recognizer.py`) —
  the `break` inside the inner `metadata` loop only exited the metadata
  iteration, not the outer `sections` loop. On multi-section Shazam responses,
  the code continued iterating through additional sections and could overwrite a
  valid album name with an empty string. Added a guard after the inner loop so
  the outer loop also exits once a non-empty album value is found.

- **Blocking cover-art download in async event loop**
  (`src/display/renderer.py`) — `urllib.request.urlretrieve()` was called
  synchronously inside `_load_cover()`, which runs on the main thread of the
  async event loop. This blocked audio capture, recognition, and all other
  async tasks for the duration of the HTTP download on each new track. Fixed by
  splitting responsibilities: `_load_cover()` now reads only from the disk
  cache and returns `None` immediately on a cache miss; a new
  `_prefetch_cover(url)` async method downloads the file in a thread-pool
  executor (`run_in_executor`) and is scheduled via `asyncio.create_task()`
  from `_on_state_change()`. Cover art loads asynchronously; a brief
  placeholder is shown if the cache miss occurs.

- **Pulsing NOW PLAYING dot froze after ~1 second**
  (`src/display/renderer.py`) — the animated `●` dot in the header strip is
  driven by `time.monotonic()` inside `_render_now_playing()`, but `_dirty`
  was never set to `True` after the initial render, so the render loop went
  idle and the animation froze. Added `self._dirty = True` at the end of
  `_render_now_playing()` to keep the loop re-rendering while the now-playing
  screen is active.

- **Genre chip overflow allowed an extra row** (`src/display/renderer.py`) —
  the bounding-box overflow check in the chip grid renderer used
  `y + chip_h > rect.y + rect.h + chip_h`, which permitted chips to overflow
  by a full `chip_h` before breaking. Changed to `y + chip_h > rect.y + rect.h`
  to clip correctly at the panel boundary.

- **Inconsistent color tuple for NEXT track label**
  (`src/display/renderer.py`) — the NEXT track name was rendered with
  `(*p.text[:3],)` (an unpacked 3-element slice wrapped in a tuple) while the
  PREV track name used `p.text` directly. Both are semantically identical when
  `p.text` is already a 3-tuple, but the NEXT label form was inconsistent and
  fragile if `DisplayPalette.text` were ever changed to a longer tuple. Changed
  to `p.text` to match the PREV label.

- **Wrong Last.fm auth URL in `get_lastfm_session_key.py`** — the help text
  printed at startup referenced `https://www.last.fm/api/accounts`, which
  returns a 404. Corrected to `https://www.last.fm/api/account/create`.

- **Negative sleep duration in `AudioCapture.run()`**
  (`src/audio/capture.py`) — if `overlap_seconds >= chunk_seconds` (a
  pathological but reachable config combination), `chunk_seconds -
  overlap_seconds` is negative and `asyncio.sleep()` raises a `ValueError`.
  The duration is now clamped: `await asyncio.sleep(max(0, chunk_seconds -
  overlap_seconds))`.

---

## [1.3.0] — 2026-05-25

### Added

- **Last.fm scrobbling** — every track confirmed by the recognition loop is
  automatically scrobbled to Last.fm. Scrobbles include artist, title, album,
  and the Unix timestamp of when the track was committed. Enabled via the new
  `lastfm.scrobble_enabled` config key (default `false`).
- **"Loved" mark on album completion** — when `love_on_completion: true` is
  set in config and a full album side plays through (i.e. `potential_last_track`
  fires), the last identified track is marked as Loved on Last.fm. Off by
  default. Failure is non-fatal and logged as a warning.
- **`src/tracking/lastfm_client.py`** — new `LastFmClient` class wrapping
  `pylast`. Synchronous (pylast is synchronous); async callers use
  `run_in_executor`, matching the `DiscogsClient` pattern. Graceful no-op when
  not configured or when pylast is not installed. No exception ever propagates
  out of this module — every failure is caught and returned as `False`.
- **`get_lastfm_session_key.py`** — one-time helper script at the repo root.
  Walks through the Last.fm desktop auth flow (token → browser approval →
  session key), then prints the session key to paste into `config.yaml`. The
  session key does not expire; the script only needs to be run once.
- New `lastfm` section in `config.example.yaml`:
  `scrobble_enabled`, `api_key`, `api_secret`, `session_key`, `love_on_completion`.
- **`pylast>=5.1.0`** added to `requirements.txt`.
- **15 new unit tests** in `tests/test_lastfm_client.py` covering: disabled
  config, missing config section, incomplete credentials, pylast ImportError,
  scrobble happy path, empty album → `None`, scrobble when disabled, scrobble
  exception handling, love happy path, love disabled by config, love when
  client disabled, love exception handling, `enabled` property, `love_on_completion`
  property, and full-credentials → enabled.
  Total unit test count: 193 → 208.

### Changed

- `RecognitionLoop.__init__` — accepts an optional `lastfm: LastFmClient`
  parameter (default `None`; backward-compatible).
- `RecognitionLoop._commit_track()` — records a Unix timestamp before
  resolving metadata, then fires `lastfm.scrobble()` in an executor after
  updating state and tracker. Scrobble failure is caught and logged; it never
  interrupts the main loop.
- `ListenTracker.__init__` — accepts an optional `lastfm: LastFmClient`
  parameter (default `None`; backward-compatible).
- `ListenTracker._end_session()` — after the Discogs Play Count and Last
  Played updates, calls `lastfm.love()` on the last identified track when
  `love_on_completion` is enabled. Independent of Discogs: a Discogs failure
  does not prevent the love call.
- `main.py` — constructs `LastFmClient(config)` at startup and injects it
  into both `ListenTracker` and `RecognitionLoop`.
- Module docstring for `listen_tracker.py` updated to document the Last.fm
  love step in the session-end logic.

---

## [1.2.2] — 2026-05-25

### Fixed

- **Cross-side boundary bug in `prev_track_title` / `next_track_title`** — both
  properties previously searched only within the current side's entries. This
  caused the first track on any non-first side (e.g. B1) to return `None` for
  `prev_track_title` instead of the last track of the preceding side (e.g. A3),
  and the last track on any non-last side (e.g. A3) to return `None` for
  `next_track_title` instead of the first track of the following side (e.g. B1).
  Both properties now fall back to the global tracklist when a side boundary is
  reached, correctly stitching sides together. A track that is genuinely first
  globally still returns `None` for `prev_track_title`; a track that is genuinely
  last globally still returns `None` for `next_track_title`.
- New unit tests cover the fixed behaviour:
  `test_prev_track_cross_side_b1_returns_last_of_a` (B1 prev → A3),
  `test_next_track_cross_side_last_a_returns_first_of_b` (A3 next → B1),
  `test_prev_track_very_first_track_is_none` (A1 has no predecessor),
  and `test_next_track_very_last_track_is_none` (B4 has no successor).
  Several pre-existing boundary tests were renamed for specificity; net
  test count: 192 → 193.

---

## [1.2.1] — 2026-05-25

### Changed

- **Dynamic title push-down layout** — the track title is now the unconstrained
  hero element. Instead of occupying a fixed 170px slot and scaling down when text
  overflows, the title takes as much vertical space as it naturally requires. The
  accent divider, artist name, album title, and genre chip badges then flow
  downward from the title's actual bottom edge. The meta footer and prev/next strip
  remain bottom-anchored and are never displaced.  Font size reduction is a last
  resort, applied only when the title genuinely cannot fit even after the secondary
  block has been pushed as far down as possible (i.e. the full budget is consumed).
- `_draw_wrapped_text()` now returns the actual rendered height in pixels so callers
  can position subsequent elements relative to the measured bottom edge.
- New `_measure_wrapped_text()` helper computes wrapped-text height without drawing,
  using the same word-wrap algorithm as `_draw_wrapped_text()` to ensure consistent
  measurement vs. render output.
- `_draw_genre_chips()` accepts an optional `chips_rect` parameter; when supplied it
  overrides `layout.genre_chips` for positioning, enabling dynamic y-coordinate injection.
- `_build_font_cache()` pre-builds stepped-down bold font variants (4 px steps from
  the default title size down to 18 px) into a new `_bold_fonts` dict, used by
  the title-scaling fallback in `_render_now_playing()`.

---

## [1.2.0] — 2026-05-25

### Added

- **"Museum Card" display redesign** — completely new layout derived from Claude Design
  mockups (DirectionA variant): cover art on the left (~440×440px), text panel on the
  right with a hero-scale track title (72px bold), a short accent divider line, artist
  name (48px), album name (32px italic serif), genre/style chip badges, a compact meta
  footer (year · label · catalog), and a prev/next track strip anchored to the bottom.
  A full-width header strip at the top shows a pulsing NOW PLAYING dot and the current
  side/position indicator (e.g. `SIDE A · 02 OF 03`).
- **Dynamic color theming** — album art is quantized to 8 colors via Pillow on each
  track change; the most vibrant color becomes the `accent`, the dominant color is
  darkened to `bg` and `surface`, and near-white tints produce `text` and `muted`. The
  five-field `DisplayPalette` dataclass carries the resolved theme. Palettes are cached
  per cover-art URL so extraction only runs once per album.
- **Radial gradient background** — concentric-circle approximation of a center-to-edge
  gradient (surface color at center → bg color at edges) rendered each frame during
  palette transitions; no new runtime dependencies (pure pygame).
- **1-second palette lerp transitions** — when a new track arrives, the renderer
  smoothly blends `_current_palette` → `_target_palette` over 1 second using
  `_lerp_color()` / `_lerp_palette()`. The run loop continues re-rendering until the
  transition completes, then returns to dirty-flag mode.
- **Genre/style chip badges** — Discogs `styles` (prepended) plus `genres` rendered as
  pill badges with 1px solid border, configurable padding, gap, and corner radius.
  Chips wrap to a second row when they overflow the panel width.
- **Word-wrapped hero track title** — title text is manually word-wrapped across
  multiple lines at the panel width; line height is 0.98× the font height.
- **Side-awareness properties on `TrackMetadata`** — five new computed properties
  derived from the tracklist: `side_letter` (e.g. `"A"`), `side_position` (1-based
  index within the side), `side_total` (track count for that side), `prev_track_title`,
  and `next_track_title`. All return `None` when the track is not found in the
  tracklist or has a numeric-only position string.
- **`genres` field on `TrackMetadata`** — Discogs `styles` followed by `genres` are
  concatenated into a single `genres: list[str]` field. No new API calls — both fields
  are already present in the release response; only extraction was added.
- **`DisplayPalette` dataclass** and **`FALLBACK_PALETTE`** constant in `models.py` —
  a neutral dark-grey fallback used when cover art is missing or extraction fails.
- **`_SIDE_RE` regex** exported from `models.py` — `r"^([A-Za-z]+)(\d+)$"` — parses
  Discogs position strings (e.g. `"B12"`) into `(side_letter, track_number)`.
- **44 new unit tests** across `test_models.py`, `test_layouts.py`, and
  `test_resolver.py` covering all new properties, layout geometry invariants (bounds,
  ordering, font hierarchy, scaling), and genres passthrough.

### Changed

- `NowPlayingLayout` — entirely new field set: 9 layout rects (`header_strip`,
  `cover_art`, `track_text`, `divider`, `artist_text`, `album_text`, `genre_chips`,
  `meta_text`, `prev_next`), 7 font sizes, and 5 chip geometry constants. The old
  3-column single-line layout is replaced by the Museum Card design.
- `get_now_playing_layout()` — all geometry now scales from a 1024×600 reference;
  cover art forced square via `min(sx, sy)` scaling to prevent distortion at non-16:9
  resolutions.
- `DisplayRenderer` — complete rewrite: three font dicts (`_fonts`, `_italic_fonts`,
  `_mono_fonts`) built at startup; dynamic palette fields wired into every draw call;
  radial gradient replaces solid fill; six new private draw methods.
- `DiscogsClient._build_result()` — now extracts `release.styles` (prepended) and
  `release.genres` into a combined `genres` list in the return dict.
- `MetadataResolver._from_discogs()` — passes `genres` through to `TrackMetadata`.
- Total unit test count: 148 → 192.

---

## [1.1.0] — 2026-05-24

### Added

- **Last Played date tracking** — on album completion, `DiscogsClient.update_last_played()`
  writes today's date (ISO 8601, `YYYY-MM-DD`) to a configurable "Last Played" custom
  field in the user's Discogs collection. The field is optional: if
  `discogs.last_played_field_name` is not set in `config.yaml`, the method is a
  graceful no-op and no API calls are made.
- `config.example.yaml` — added optional `last_played_field_name` key (commented out
  by default) with instructions for enabling it.
- 7 new unit tests in `tests/test_discogs_client.py` covering `update_last_played`
  (not configured no-op, happy path, ISO date format verification, field not found,
  non-204 POST, 401, exception handling).
- 3 new unit tests in `tests/test_listen_tracker.py` covering Last Played integration
  (called when configured, not called when unconfigured, failure is non-fatal).

### Changed

- `ListenTracker._end_session()` now calls `update_last_played()` after
  `increment_play_count()` when `last_played_field_name` is configured. A failure
  from `update_last_played` is logged as a warning but does not affect the Play Count
  result — the two updates are independent.
- Log message updated: "incrementing Play Count in Discogs" →
  "incrementing Play Count and updating Last Played in Discogs".
- Total unit test count: 138 → 148.

---

## [1.0.1] — 2026-05-24

### Changed

- **Play Count replaces "Listened?" boolean** — `DiscogsClient.mark_as_listened()`
  (which set a dropdown field to "Yes") is replaced by `increment_play_count()`,
  which reads the current integer value of a "Play Count" custom field and
  increments it by 1. An empty Play Count field implies unlistened, making the
  separate boolean redundant.
- `discogs.listened_field_name` and `discogs.listened_field_value` config keys
  replaced by a single `discogs.play_count_field_name` key.
- `ListenTracker` updated to call `increment_play_count()` instead of
  `mark_as_listened()`; log messages updated accordingly.

### Added

- `DiscogsClient._get_field_value()` — reads the current raw value of a custom
  field from the collection API response, used by `increment_play_count()` to
  determine the value before incrementing (read-before-write pattern; falls back
  to 0 on GET failure or blank field).
- `tests/test_discogs_client.py` — new unit test file covering 14 scenarios for
  `increment_play_count` and `_get_field_value` (blank field, existing counts,
  garbage values, field-not-found, GET/POST failures, exceptions).

---

## [1.0.0] — 2026-05-24

Initial release. Full core loop operational: turntable audio → Shazam
recognition → Discogs metadata → pygame display → Discogs field update.

### Added

**Audio pipeline**
- `AudioCapture` — records overlapping 15s chunks from USB audio interface
  via `sounddevice`; dispatches to silence detector and recognition queue
- `SilenceDetector` — RMS-based silence/music classification; emits
  `MUSIC_STARTED`, `MUSIC_STOPPED`, and `SESSION_ENDED` lifecycle events;
  `SESSION_ENDED` requires sustained silence after music (default 45s) and
  fires at most once per session

**Recognition**
- `RecognitionLoop` — async polling loop with configurable N-of-consecutive-
  matches confirmation gate (default 2) to prevent flickering on noisy results
- `ShazamIOBackend` — serialises audio to in-memory WAV, calls ShazamIO;
  swappable via `recognition.backend` config key (ACRCloud and AudD stubs ready)

**Metadata**
- `MetadataResolver` — three-tier lookup chain: Discogs collection →
  Discogs database → MusicBrainz/Shazam fallback; always returns a
  `TrackMetadata` regardless of which tier succeeds
- `DiscogsClient` — collection search with 25-candidate database cross-
  reference strategy plus full collection-walk fallback for rare pressings;
  custom field update via Discogs REST API
- `CoverArtFallback` — MusicBrainz Cover Art Archive lookup for releases
  not found in Discogs

**Display**
- `DisplayRenderer` — pygame fullscreen renderer at configurable resolution
  (default 1024×600 for Waveshare 7" HDMI LCD H); dirty-flag redraw at ~30fps
- Three screens: idle (dark), listening ("Listening…"), now-playing (cover
  art + artist / album / track / meta / position / source badge)
- `NowPlayingLayout` — proportional pixel geometry; resolution-independent;
  scales correctly at 640×480, 800×480, 1024×600, 1280×720
- Cover art downloaded from Discogs/MusicBrainz URLs with MD5-keyed disk cache
- Fallback source indicator badge when metadata comes from MusicBrainz

**State & tracking**
- `PlayerState` — central in-memory state with observer pattern;
  status enum: `IDLE → LISTENING → PLAYING → IDLE`
- `ListenTracker` — manages `PlaySession` lifecycle; updates Discogs field
  only when last track is confirmed AND release is in collection (conservative
  by design — partial plays do not trigger an update)
- `PlaySession` — deduplicates consecutive track logs; latches release/instance
  IDs from the first Discogs-sourced track

**Infrastructure**
- `VERSION` file at repo root; `main.py` logs version at startup
- GitHub Actions workflow auto-syncs README version badge when `VERSION` changes
- 124-test unit suite covering all non-hardware components (models, silence
  detection, listen tracker, metadata resolver, recognition loop, display layout)
- `test_discogs_live.py` — live Discogs integration test with read-only and
  `--test-write` modes; tests collection search, database search, tracklist
  fetch, custom field detection, and field update

**Documentation**
- `docs/architecture.md` — full system design, component reference, data flows,
  state machine, config reference
- `docs/testing-guide.md` — prerequisites, test inventory, run commands,
  per-suite descriptions, common failure modes
- `docs/pi-setup-guide.md` — OS flash, display config, UCA222 setup, venv,
  first run, systemd autostart, troubleshooting
- `docs/hardware-guide.md` — parts list and wiring diagram
- `docs/roadmap.md` — versioned feature plan through v1.6.0
