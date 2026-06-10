# Raspberry Pi 4 Setup Guide — vinyl-now-playing

Everything you need to go from a bare Pi 4 to a running vinyl tracker.
Hardware assumed: **Raspberry Pi 4**, **Waveshare 7" HDMI LCD (H)** (1024×600),
**Behringer UCA222** USB audio interface.

---

## 1. Flash the OS

Use **Raspberry Pi Imager** (download at [raspberrypi.com/software](https://www.raspberrypi.com/software/)).

**Choose OS:** Raspberry Pi OS (64-bit) — the full Desktop version, not Lite.
pygame needs a desktop environment. If you want a minimal install you can add
LXDE later, but the full image is easier.

**Before writing**, click the gear icon in Imager and pre-configure:
- Hostname: `vinylpi` (or whatever you like)
- Enable SSH (password or public key — your choice)
- Wi-Fi SSID + password
- Username: `pi` (default) and a password

Write to your SD card, insert into the Pi, and power on.

---

## 2. First boot — SSH in

```bash
ssh pi@vinylpi.local
```

If `.local` doesn't resolve, find the IP from your router and use that instead.

Update everything before installing anything:

```bash
sudo apt update && sudo apt upgrade -y
```

---

## 3. Configure the Waveshare display

The Waveshare 7" HDMI LCD (H) is plug-and-play over HDMI — no driver needed.
You just need to tell the Pi to output at its native resolution.

Edit the boot config:

```bash
sudo nano /boot/config.txt
```

Find the `[all]` section (or add it at the bottom) and set:

```ini
# Waveshare 7" HDMI LCD (H) — 1024×600
hdmi_group=2
hdmi_mode=87
hdmi_cvt=1024 600 60 6 0 0 0
hdmi_drive=1
```

Save and reboot:

```bash
sudo reboot
```

After rebooting, SSH back in and verify the resolution:

```bash
DISPLAY=:0 xrandr | head -5
```

You should see `1024x600` listed as the current mode. If the screen is blank,
try `hdmi_drive=2` instead (some monitors need HDMI with audio signalling).

---

## 4. Install system dependencies

```bash
# Audio (required by sounddevice)
sudo apt install -y libportaudio2 portaudio19-dev

# pygame display dependencies
sudo apt install -y libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev

# Pillow image processing (required for dynamic color theming)
# These provide JPEG/PNG decode support when Pillow compiles from source on the Pi
sudo apt install -y libjpeg-dev libpng-dev

# Git (usually pre-installed, but just in case)
sudo apt install -y git

# Python build tools
sudo apt install -y python3-pip python3-venv python3-dev
```

---

## 5. Verify the UCA222 is recognised

Plug the UCA222 into a USB port on the Pi, then:

```bash
aplay -l
```

You should see something like:

```
card 1: CODEC [USB Audio CODEC], device 0: USB Audio [USB Audio]
```

If it's not there, try a different USB port or check the cable. The device name
`USB Audio Codec` is what's already set in `config.example.yaml` — confirm the
name matches what `aplay -l` shows.

To verify the input (capture) side:

```bash
arecord -l
```

You should see the same card listed as a capture device. If you want to do a
quick sanity check before running the full app:

```bash
arecord -D hw:1,0 -f S16_LE -r 44100 -d 5 /tmp/test.wav && aplay /tmp/test.wav
```

This records 5 seconds from the UCA222 and plays it back. Play something on
your turntable while it records — you should hear it played back. (Adjust the
card index `hw:1,0` if `arecord -l` shows the UCA222 on a different card number.)

---

## 6. Clone the repo and set up the Python environment

```bash
cd ~
git clone https://github.com/lanebecker/vinyl-now-playing.git
cd vinyl-now-playing

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

Installation will take a few minutes on the Pi — numpy and pygame both compile
native extensions.

---

## 7. Create config.yaml

```bash
cp config.example.yaml config.yaml
nano config.yaml
```

Key values to fill in:

| Key | What to set |
|-----|------------|
| `audio.device_name` | Must match what `aplay -l` showed — default `"USB Audio Codec"` is usually correct |
| `discogs.user_token` | From discogs.com → Settings → Developers → Generate token |
| `discogs.username` | Your Discogs username |
| `discogs.play_count_field_name` | Must match your "Play Count" custom field name **exactly** (case-sensitive) |
| `discogs.last_played_field_name` | **Optional.** If you have a "Last Played" custom field in your Discogs collection, set this to match its name exactly. Leave it commented out (the default) if you don't want this feature. |
| `lastfm.scrobble_enabled` | Set to `true` to enable Last.fm scrobbling. Also fill in `api_key`, `api_secret`, and `session_key` (see step 8 below). |
| `lastfm.api_key` | Your Last.fm API key — see step 8. |
| `lastfm.api_secret` | Your Last.fm shared secret — see step 8. |
| `lastfm.session_key` | Generated once via `python get_lastfm_session_key.py` — see step 8. Does not expire. |
| `lastfm.love_on_completion` | **Optional.** If `true`, marks the last identified track as Loved on Last.fm when a full album side plays through. Defaults to `false`. |
| `display.dynamic_theming` | Defaults to `true`. Extracts a 5-color palette from each album's cover art (Pillow) and shifts the background/accent colors per record. Set to `false` if you prefer a fixed dark theme or notice performance issues on older Pi hardware. |

Everything else can stay as-is for a first run.

---

## 8. Set up Last.fm scrobbling (optional)

Skip this step if you don't have a Last.fm account or don't want scrobbling.

### 8a. Create a Last.fm API application

1. Log into [last.fm](https://www.last.fm) with your account.
2. Go to [last.fm/api/account/create](https://www.last.fm/api/account/create).
3. Fill in:
   - **Application name:** anything you like (e.g. `Vinyl Now Playing`)
   - **Application description:** brief description
   - **Callback URL:** leave blank (the app uses the desktop auth flow, not web OAuth)
4. Submit. You'll land on a page showing your **API key** and **Shared secret** — copy both into your `config.yaml` under `lastfm.api_key` and `lastfm.api_secret`.

### 8b. Generate a session key

Last.fm requires a one-time authorisation step to generate a session key. The
session key never expires, so this only needs to be done once.

With the venv active, run the helper script from the repo root:

```bash
source venv/bin/activate
python get_lastfm_session_key.py
```

The script will:
1. Prompt you to paste your API key and shared secret
2. Open the Last.fm authorisation page in your browser
3. Wait for you to approve access on the Last.fm site
4. Print your session key to the terminal

Copy the session key into `config.yaml` under `lastfm.session_key`.

### 8c. Complete the config.yaml lastfm section

Your `config.yaml` should now contain:

```yaml
lastfm:
  scrobble_enabled: true
  api_key: "your-api-key-here"
  api_secret: "your-shared-secret-here"
  session_key: "your-session-key-here"
  love_on_completion: false   # set to true to mark last track as Loved on album completion
```

### 8d. Verify the connection

Confirm all three credentials are valid and talking to the Last.fm API:

```bash
python -c "
import pylast
network = pylast.LastFMNetwork(
    api_key='YOUR_API_KEY',
    api_secret='YOUR_API_SECRET',
    session_key='YOUR_SESSION_KEY',
    username='YOUR_LASTFM_USERNAME'
)
user = network.get_authenticated_user()
print(f'Authenticated as: {user.get_name()}')
print(f'Play count: {user.get_playcount()}')
"
```

Replace the four placeholder values with your actual credentials. You should see
your Last.fm username and total scrobble count printed. A `WSError` means
something is wrong with the credentials — double-check that all three values
were copied correctly from the API account page and the session key helper output.

---

## 9. Verify Discogs credentials

Before dealing with audio and display, confirm the Discogs side works:

```bash
python test_discogs_live.py
```

All four read-only tests should pass. If test 1 (search_collection) misses, see
`docs/testing-guide.md` — the album strings at the top of the script may need
adjusting to match a record you actually own.

---

## 10. Run a Python device check

Confirm sounddevice sees the UCA222 at the Python level:

```python
source venv/bin/activate
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

Look for a line containing `USB Audio Codec` (or similar). Note whether it
appears as an input device — it should show a positive number of input channels.

---

## 11. First manual run

With the display connected, the UCA222 plugged in, and your turntable's RCA
output going into the UCA222's inputs:

```bash
cd ~/vinyl-now-playing
source venv/bin/activate
DISPLAY=:0 python3 main.py
```

The display should open showing an idle/waiting state. Drop the needle on a
record — within roughly 25–40 seconds the track name and album art should
appear (capture is continuous: the first 15s recognition window completes at
15s and the second at 25s, satisfying the two-consecutive-matches gate; the
rest is Shazam round-trip time).

Watch the terminal output for log messages. These are the INFO-level lines
the app actually emits, in the order you should see them:

- `Play session started.` — the silence detector heard audio above the threshold
- `Track confirmed: <artist> — <title>` — the confirmation gate passed (two consecutive matching results)
- `Now playing: <artist> / <album> / <title> [DISCOGS_COLLECTION]` — metadata resolved; the bracketed source shows which lookup tier succeeded (`DISCOGS_COLLECTION` means your own pressing was found; `DISCOGS_DATABASE` or `FALLBACK` mean it wasn't)
- `Last.fm scrobbled: <artist> — <title>` — the track was posted to your Last.fm listening history (if scrobbling is enabled)
- `Last track of album identified: ...` — the album closer was recognized; completion updates will fire at session end
- `Play Count updated for release ...` and `✅ Discogs Play Count incremented successfully.` — the Play Count field was incremented at end of session
- `Last Played updated for release ...` — the Last Played date was written (only if `last_played_field_name` is configured)
- `✅ Last.fm loved: ...` — the last track was marked as Loved on Last.fm (only if `love_on_completion: true`)

(Chunk-level events such as `SilenceDetector → MUSIC_STARTED` and per-tier
Discogs lookup details are logged at DEBUG level — change `level=logging.INFO`
to `logging.DEBUG` in `main.py` if you need them while troubleshooting.)

If `Play session started.` never appears, the silence threshold may be too high for
your room's noise floor. Tune `audio.silence_threshold_rms` in config.yaml —
lower values are more sensitive (0.005 is a reasonable starting point).

To exit: `Ctrl+C`.

---

## 12. Set up autostart with systemd

Once the manual run works, set it up to start automatically on boot.

Create the service file:

```bash
sudo nano /etc/systemd/system/vinyl-now-playing.service
```

Paste this (adjust the username if you're not using `pi`):

```ini
[Unit]
Description=vinyl-now-playing
After=network.target graphical.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/vinyl-now-playing
Environment="DISPLAY=:0"
Environment="XAUTHORITY=/home/pi/.Xauthority"
ExecStart=/home/pi/vinyl-now-playing/venv/bin/python3 main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable vinyl-now-playing
sudo systemctl start vinyl-now-playing
```

Check status:

```bash
sudo systemctl status vinyl-now-playing
```

View live logs:

```bash
journalctl -u vinyl-now-playing -f
```

---

## 13. Optional: hide the desktop and boot straight to the app

If you want the Pi to boot directly to the vinyl display with no desktop
visible underneath:

**Auto-login to desktop** (if not already set):

```bash
sudo raspi-config
# System Options → Boot / Auto Login → Desktop Autologin
```

**Disable the screensaver and power blanking** so the display stays on:

```bash
sudo nano /etc/xdg/lxsession/LXDE-pi/autostart
```

Add these lines:

```
@xset s off
@xset -dpms
@xset s noblank
```

The app runs fullscreen (set in config.yaml: `display.fullscreen: true`) so the
desktop will be hidden behind it automatically once the service starts.

---

## Troubleshooting

**Display is blank / wrong resolution**
Check `/boot/config.txt` — verify `hdmi_cvt=1024 600 60 6 0 0 0` is set and
there are no conflicting `hdmi_mode` lines earlier in the file. Try swapping
`hdmi_drive=1` to `hdmi_drive=2`.

**`OSError: PortAudio library not found`**
Run `sudo apt install -y libportaudio2` and try again.

**`sounddevice` can't find the UCA222**
Run `python3 -c "import sounddevice; print(sounddevice.query_devices())"` and
check the exact device name. Update `audio.device_name` in config.yaml to match.

**`Play session started.` never appears**
The input level is too quiet. Either the turntable volume is low, or
`silence_threshold_rms` is set too high. Try lowering it to `0.005`. You can
also run the arecord sanity check from step 5 to confirm audio is reaching the Pi.

**Recognition never commits a track**
Check that you have internet connectivity (`ping 8.8.8.8`). ShazamIO makes
outbound HTTPS requests. Also confirm the chunk timing — capture is
continuous (v1.3.3), with a `chunk_seconds: 15` window sent for recognition
every `chunk_seconds - overlap_seconds` (10s by default). With
`confirmation_required: 2`, the first commit takes ~25 seconds after the
needle drops (first window completes at 15s, second at 25s), plus Shazam
round-trip time.

**`Discogs 401 Unauthorized`**
Your `user_token` is invalid or expired. Generate a new one at
discogs.com/settings/developers.

**systemd service fails to start**
Check `journalctl -u vinyl-now-playing -n 50` for the actual error. Common
causes: `DISPLAY` not set (add `Environment="DISPLAY=:0"` to the service file),
or the venv path is wrong (verify with `which python3` inside the activated venv).

**App starts but pygame window is invisible**
The service may be starting before the desktop is fully up. Add
`After=graphical-session.target` to `[Unit]` in the service file and reload.
