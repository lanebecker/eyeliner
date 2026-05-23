# Architecture

See the full design document: [vinyl-now-playing-architecture.md](../vinyl-now-playing-architecture.md)

## Quick Reference

```
Turntable → USB Audio Interface → Raspberry Pi
  └── AudioCapture (sounddevice)
        ├── SilenceDetector  → PlayerState (IDLE / LISTENING / PLAYING / SESSION_ENDED)
        └── RecognitionLoop (ShazamIO)
              └── MetadataResolver
                    ├── 1. Discogs Collection
                    ├── 2. Discogs Database
                    └── 3. MusicBrainz fallback
              ├── DisplayRenderer (pygame, HDMI)
              └── ListenTracker → Discogs "Listened to?" field
```

## Component Files

| File | Responsibility |
|---|---|
| `main.py` | Entry point, wires all components, starts async event loop |
| `src/audio/capture.py` | Records audio from USB interface in overlapping chunks |
| `src/audio/silence.py` | RMS-based silence detection, emits AudioEvents |
| `src/audio/recognizer.py` | ShazamIO polling loop, confirmation logic, backend interface |
| `src/metadata/models.py` | TrackMetadata, PlaySession, MetadataSource dataclasses |
| `src/metadata/discogs_client.py` | Discogs collection/DB search, custom field update |
| `src/metadata/coverart.py` | MusicBrainz Cover Art Archive fallback |
| `src/metadata/resolver.py` | Orchestrates 3-step lookup chain |
| `src/state/player_state.py` | Central state, status transitions, change listeners |
| `src/display/layouts.py` | Pixel positions and font sizes (restyle here) |
| `src/display/renderer.py` | pygame window management, cover art cache |
| `src/tracking/listen_tracker.py` | Play session tracking, Discogs "Listened to?" update |

## What Needs Implementing

The three `NotImplementedError` stubs in `src/metadata/discogs_client.py` are the main
remaining work before the app is functional:

1. `search_collection()` — search the user's Discogs collection by artist + album
2. `search_database()` — search the full Discogs release database
3. `mark_as_listened()` — POST to the collection field update endpoint

All other components are functionally complete.
