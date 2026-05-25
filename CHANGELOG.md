# Changelog

All notable changes to vinyl-now-playing are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versions follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

---

## [Unreleased]

_Nothing yet. Add notes here as features are built, then move them under a
new version heading when VERSION is bumped._

---

## [1.2.0] — 2026-05-25

### Added

- **“Museum Card” display redesign** — completely new layout derived from Claude Design
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
