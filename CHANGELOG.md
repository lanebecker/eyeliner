# Changelog

All notable changes to vinyl-now-playing are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versions follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

---

## [Unreleased]

_Nothing yet. Add notes here as features are built, then move them under a
new version heading when VERSION is bumped._

---

## [1.0.0] — 2026-05-24

Initial release. Full core loop operational: turntable audio → Shazam
recognition → Discogs metadata → pygame display → Discogs "Listened to?"
field update.

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
- `ListenTracker` — manages `PlaySession` lifecycle; marks Discogs
  "Listened to?" field only when last track is confirmed AND release is in
  collection (conservative by design — partial plays do not trigger an update)
- `PlaySession` — deduplicates consecutive track logs; latches release/instance
  IDs from the first Discogs-sourced track

**Infrastructure**
- `VERSION` file at repo root; `main.py` logs version at startup
- GitHub Actions workflow auto-syncs README version badge when `VERSION` changes
- 124-test unit suite covering all non-hardware components (models, silence
  detection, listen tracker, metadata resolver, recognition loop, display layout)
- `test_discogs_live.py` — live Discogs integration test with read-only and
  `--test-write` modes; tests collection search, database search, tracklist
  fetch, custom field detection, and `mark_as_listened`

**Documentation**
- `docs/architecture.md` — full system design, component reference, data flows,
  state machine, config reference
- `docs/testing-guide.md` — prerequisites, test inventory, run commands,
  per-suite descriptions, common failure modes
- `docs/pi-setup-guide.md` — OS flash, display config, UCA222 setup, venv,
  first run, systemd autostart, troubleshooting
- `docs/hardware-guide.md` — parts list and wiring diagram
- `docs/roadmap.md` — versioned feature plan through v1.6.0
