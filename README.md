# vinyl-now-playing

[![version](https://img.shields.io/badge/version-1.0.0-blueviolet)](VERSION)

A Raspberry Pi app that listens to a vinyl record playing through a USB audio interface, identifies the current track via audio fingerprinting, enriches it with metadata from your Discogs collection, and displays the artist, album, track name, and cover art on an HDMI-connected LCD screen.

When the last track of an album finishes, it automatically marks that record as "Listened to?" in your Discogs collection.

## Features

- 🎵 Real-time audio fingerprinting via ShazamIO (no manual input needed)
- 💿 Discogs collection-first metadata — pulls your specific pressing's details
- 🖼️ Full cover art display on any HDMI screen
- ✅ Automatically marks records as listened in Discogs when the last track plays
- 🔄 Graceful fallback: Discogs collection → Discogs database → MusicBrainz
- 🔧 Swappable recognition backend (ShazamIO, ACRCloud, AudD)

## Hardware

- Raspberry Pi 4 Model B (4GB recommended)
- USB audio interface (e.g. Behringer UCA222) connected to your turntable preamp's line-level output
- HDMI LCD screen — built and tested with Waveshare 7" HDMI LCD (H) at 1024×600

## Setup

```bash
git clone https://github.com/lanebecker/vinyl-now-playing.git
cd vinyl-now-playing
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Edit config.yaml with your Discogs token, username, and audio device
python main.py
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in:

- `discogs.user_token` — from https://www.discogs.com/settings/developers
- `discogs.username` — your Discogs username
- `audio.device_name` — run `python -c "import sounddevice; print(sounddevice.query_devices())"` to find your USB interface name
- `discogs.listened_field_name` — the exact name of your custom field (default: `"Listened to?"`)

## Documentation

- [Architecture](docs/architecture.md) — full system design, component reference, data flows
- [Roadmap](docs/roadmap.md) — planned features and versioning
- [Testing guide](docs/testing-guide.md) — running the unit and integration test suites
- [Pi setup guide](docs/pi-setup-guide.md) — hardware bring-up from bare Pi to running app
- [Hardware guide](docs/hardware-guide.md) — wiring diagram and parts list

## Inspired By

- [VinylPi64](https://github.com/simontrost/VinylPi64) by simontrost
- [shazampi-eink](https://github.com/ravi72munde/shazampi-eink) by ravi72munde

## License

MIT
