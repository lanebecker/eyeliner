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

Everything else can stay as-is for a first run.

---

## 8. Verify Discogs credentials

Before dealing with audio and display, confirm the Discogs side works:

```bash
python test_discogs_live.py
```

All four read-only tests should pass. If test 1 (search_collection) misses, see
`docs/testing-guide.md` — the album strings at the top of the script may need
adjusting to match a record you actually own.

---

## 9. Run a Python device check

Confirm sounddevice sees the UCA222 at the Python level:

```python
source venv/bin/activate
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

Look for a line containing `USB Audio Codec` (or similar). Note whether it
appears as an input device — it should show a positive number of input channels.

---

## 10. First manual run

With the display connected, the UCA222 plugged in, and your turntable's RCA
output going into the UCA222's inputs:

```bash
cd ~/vinyl-now-playing
source venv/bin/activate
DISPLAY=:0 python3 main.py
```

The display should open showing an idle/waiting state. Drop the needle on a
record — within 30–60 seconds (two consecutive recognition cycles) the track
name and album art should appear.

Watch the terminal output for log messages. Key things to look for:

- `MUSIC_STARTED` — the silence detector is hearing audio above the threshold
- `RawRecognitionResult` — Shazam identified a track
- `Committed track:` — confirmation count reached, track is live on screen
- `Found in collection` — the Discogs lookup succeeded
- `Play Count updated for release ...` — the field update fired at end of session

If `MUSIC_STARTED` never appears, the silence threshold may be too high for
your room's noise floor. Tune `audio.silence_threshold_rms` in config.yaml —
lower values are more sensitive (0.005 is a reasonable starting point).

To exit: `Ctrl+C`.

---

## 11. Set up autostart with systemd

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

## 12. Optional: hide the desktop and boot straight to the app

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

**`MUSIC_STARTED` never fires**
The input level is too quiet. Either the turntable volume is low, or
`silence_threshold_rms` is set too high. Try lowering it to `0.005`. You can
also run the arecord sanity check from step 5 to confirm audio is reaching the Pi.

**Recognition never commits a track**
Check that you have internet connectivity (`ping 8.8.8.8`). ShazamIO makes
outbound HTTPS requests. Also confirm the chunk length — at `chunk_seconds: 15`
with `confirmation_required: 2`, the first commit takes ~30 seconds after the
needle drops.

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
