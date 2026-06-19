# Product

## Register

product

## Users

Primarily the owner: a vinyl listener who wants a companion display running while records play. Secondary audience: other vinyl enthusiasts if the project is shared. Usage context is a listening room — the display runs on a Raspberry Pi connected to a turntable, shown on a Waveshare 7" HDMI LCD (H) at 1024×600, positioned near the turntable. The listener is seated across the room. The display is glanced at, not watched. It is never touched or interacted with.

## Product Purpose

A passive now-playing display for a vinyl turntable. The turntable's audio feeds a Raspberry Pi running ShazamIO (an async Python audio-fingerprinting library — no API key required), which identifies the currently playing track. Track metadata resolves through a three-tier chain: your Discogs collection first (fastest and richest — returns your specific pressing's label, catalog number, and side-aware tracklist), then the broader Discogs database, then MusicBrainz as a final fallback. The display then shows the track name, album cover, artist, album title, side position, label and catalog details, and adjacent tracks. The primary goal is to convey the track name to the room. Artist and album are secondary. All remaining metadata is tertiary.

When the last track of an album plays, the app automatically increments a Play Count custom field in your Discogs collection and optionally records a Last Played date. Every identified track is scrobbled to Last.fm.

The display is static — it shows what's playing now, not a timeline of when it will end. No progress bars, scrubbers, or elapsed time. Success is a display that makes the listening experience feel more considered: not a music app, not a widget, but something that belongs in the room alongside the turntable.

## Brand Personality

Warm, obsessive, physical. Like the handwritten notes on an inner sleeve, or a well-worn record shop. Character and specificity over surface polish. It knows a lot about the records it shows, and it shows that knowledge without showing off.

## Anti-references

- **Spotify / Apple Music** — no streaming-app aesthetic: no rounded tiles, no soft gradients, no "Now Playing" bars with scrubbers and shuffle icons. The product is not a player; it is a display.
- Generic music visualization UIs (waveforms, equalizer bars, pulsing blobs) — decoration masquerading as information.

## Design Principles

1. **Each record is its own world.** The display adapts to the album's palette, not to a fixed brand color. The Sonic Youth experience should feel different from the Aimee Mann experience.
2. **Physical vocabulary over digital.** Side A, track 04 of 06, catalog number, label name. These are not metadata fields; they are the language of records.
3. **The room is part of the design.** The display is a 7" screen (Waveshare 1024×600) viewed from across a listening room, not on a phone. Typography and contrast must hold at that distance. The track name must be legible in a single glance; everything else can require a closer look.
4. **Specificity is warmth.** "PAUSED · TONEARM UP" is warmer than "Paused". Obsessive detail is the personality, not decoration on top of it.
5. **Presence without intrusion.** The display sits quietly while the music plays. It should be noticeable when glanced at, invisible when not.

## Planned Features

- **v1.6.0 — Idle Screen:** When no record is playing, the display currently shows a minimal stripe placeholder. The planned redesign shows a grid of recently played album covers, optional clock/date, and a random collection suggestion during extended idle. Design spec TBD. (Originally slated for v1.4.0 and then v1.5.0; both slots went to other releases — the design-fidelity work and the code-review hardening release — see `docs/roadmap.md`.)
- **v1.7.0 — Side A/B Awareness:** Flip reminder on idle screen after Side A ends; Play Count will require both sides to complete before incrementing.
- **v1.8.0 — Web Dashboard:** Minimal HTTP server at port 8080 for checking now-playing from any device on the local network.

## Accessibility & Inclusion

WCAG AA minimum. The display is viewed at distance, so text contrast should be generous — target 5:1 or better for primary text. All animations must respect `prefers-reduced-motion`. Album cover alt text should identify artist and album for screen readers.
