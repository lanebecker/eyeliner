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

> ⚠️ **Which image / Python version (matters — #198).** Imager's default
> "Raspberry Pi OS (64-bit)" now flashes **Trixie (Python 3.13)**. That works
> with this project: `requirements.txt` pulls in the `audioop-lts` backport on
> 3.13 (Python 3.13 removed the stdlib `audioop` module that the recognition
> stack needs). If you'd rather run the longer-proven **Bookworm (Python 3.11)**
> base, choose it explicitly — it is now nested, not top-level:
> **Choose OS → Raspberry Pi OS (other) → Raspberry Pi OS (Legacy, 64-bit)**.
> Either image is fine; you'll confirm the Python version in step 6 before
> creating the virtual environment.

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

The Waveshare 7" HDMI LCD (H) is plug-and-play over HDMI — no driver needed. On
current Raspberry Pi OS (Bookworm/Trixie, which use the **KMS** graphics driver),
the panel's EDID normally negotiates 1024×600 with **zero configuration**. Boot
with the panel connected and check *before* changing anything.

Verify the current mode. On the default **Wayland** session use `wlr-randr`
(the legacy `xrandr` reports only an "XWAYLAND" virtual output under Wayland, so
it is misleading here):

```bash
wlr-randr                       # Wayland session (labwc/wayfire — the default)
# …or from a plain console with no graphical session:
kmsprint | grep -i mode | head
```

You should see `1024x600`. If it's already correct, skip the rest of this section.

**Only if the mode is wrong**, force it through the KMS driver on the kernel
command line. Append this token to the *single* line in
`/boot/firmware/cmdline.txt` (space-separated — do not add a newline):

```
video=HDMI-A-1:1024x600M@60D
```

```bash
sudo nano /boot/firmware/cmdline.txt   # add the video= token to the one line
sudo reboot
```

Re-check with `wlr-randr` after the reboot.

> **Why not `hdmi_group`/`hdmi_mode`/`hdmi_cvt` in `/boot/config.txt`?** Those
> legacy firmware options are ignored by the KMS driver — the default and only
> supported graphics stack on Bookworm/Trixie — and on those images
> `/boot/config.txt` is a do-not-edit stub anyway (the real file is
> `/boot/firmware/config.txt`). The `video=` cmdline entry is the KMS-era
> equivalent.

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
```

First confirm which Python this image gives you (all supported):

```bash
python3 --version
```

Expected: `Python 3.11.x` (Bookworm/Legacy), `3.12.x`, **or** `3.13.x` (Trixie,
the current default) — all three work. On 3.13 the install below automatically
pulls in the `audioop-lts` backport (#198); on 3.11/3.12 `audioop` is still in
the standard library. Then create the virtual environment and install:

```bash
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

Installation will take a few minutes on the Pi — numpy and pygame both compile
native extensions.

If recognition later shows `NO MATCH FOUND` for every record, the app will now
refuse to start with a clear message instead (it probes the recognition backend
at startup, #198) — the usual cause is a Python 3.13 venv created before this
fix; `pip install -r requirements.txt` inside the venv resolves it.

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
4. Write your session key to `lastfm_session_key.txt` (0600, owner-only) and
   print the file's path — it does **not** print the key itself (it grants write
   access to your Last.fm account, so it's kept out of terminal scrollback)

Copy the file's contents into `config.yaml` under `lastfm.session_key`, then
delete `lastfm_session_key.txt`.

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
were copied correctly from the API account page and the file the session key
helper wrote.

---

## 9. Verify Discogs credentials

Before dealing with audio and display, confirm the Discogs side works:

```bash
python scripts/discogs_live_check.py
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
Wants=network-online.target
After=network-online.target time-sync.target graphical.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/vinyl-now-playing
Environment="DISPLAY=:0"
Environment="XAUTHORITY=/home/pi/.Xauthority"
ExecStart=/home/pi/vinyl-now-playing/venv/bin/python3 main.py
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=15
TimeoutStopSec=30

[Install]
WantedBy=graphical.target
```

> ⚠️ **Wayland note (#200) — verify on your image.** This unit assumes the app can
> reach an **X11** display via `DISPLAY=:0` + `~/.Xauthority`. The app uses
> pygame/SDL2, which reaches the default Wayland session through Xwayland — but
> `/home/pi/.Xauthority` may not exist on a Wayland session, in which case SDL2
> can fail to open the display and the service won't start. After enabling the
> unit, confirm it actually comes up: `journalctl -u vinyl-now-playing -n 50`. If
> it can't open the display, the fix is session-specific (point `XAUTHORITY` at
> the running Xwayland auth file, or run this as a systemd **user** service, which
> inherits the session's `DISPLAY`/`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR`) — treat it
> as a bring-up step, since the right value depends on your flashed image.

`TimeoutStopSec=30` (CRIT-3) is the backstop for shutdown. On SIGTERM the app
cancels its legs, drains any in-flight end-of-session Discogs credit (bounded to
~10s), and shuts down both thread pools with `cancel_futures` so *queued* blocking
work is dropped. What it can't drop is a call already *running* on a worker thread
— Python can't interrupt a blocking C call — so a network request with no overall
deadline (a slow cover download, a hung Last.fm POST) could otherwise hold the
process open until systemd's 90s default. `TimeoutStopSec=30` SIGKILLs at 30s
instead: comfortably above the normal clean shutdown (drain + a ~15s socket
timeout), far below the point where a power-cut owner assumes the Pi has wedged.

`RestartPreventExitStatus=78` (R6-27) makes a **configuration** error terminal: on
a bad `config.yaml` (missing/typo'd required key, out-of-range value, an
unimplemented `recognition.backend`, or the recognition backend failing to import)
`main.py` exits `78` (`EX_CONFIG` from `sysexits.h`), and systemd then does **not**
restart — the service parks in `failed` so `journalctl` shows the one friendly
error, instead of crash-looping a fault that can never self-heal. A *transient*
crash still exits with a different non-zero code and restarts as normal.

`StartLimitIntervalSec=300` / `StartLimitBurst=10` (STAB-4; widened from 5 for
#201) is the backstop for *startup*. `Restart=on-failure` will otherwise restart
a crashing process every `RestartSec=15` **forever**, and each cold start rebuilds
the Discogs collection index from scratch — one GET per 100 records — so a
persistent crash (a config error that survives validation, a wedged dependency)
turns into a sustained hammering of the collection API that can sit on the
authenticated rate limit. The burst is **10, not 5**, and `RestartSec` is **15,
not 10**, for a reason beyond rate-limiting: the boot-time *session race* (#201).
On a cold first boot the system service can start before the autologin graphical
session is accepting display connections; at 5×10s the unit exhausted its retries
in ~50s and dropped to `failed` while the session was still coming up. 10×15s
covers ~2.5 minutes of session bring-up, so a slow SD-card boot recovers on its
own, while a *genuinely* broken boot still trips the limit (just later) rather
than pinning the API. (Note: a wrong or absent
`audio.device_name` is **no longer** one of these startup-crash causes. Since #164
the device lookup happens *inside* the capture retry loop, so a mistyped name or a
turntable unplugged at boot degrades to the same in-process backoff-and-retry path
a mid-run unplug takes — the process stays up, re-resolving the device each attempt
— rather than raising out of `run()` and crash-looping under systemd. The backstop
below is therefore for genuinely fatal boots, not for a fixable audio-device typo.)
These two directives tell systemd to stop
retrying once the service has been started **more than 10 times within 300
seconds**: it refuses the next start, drops the unit into a `failed` state
(`journalctl` shows `start-limit-hit`), and stops trying — so a genuinely broken
boot goes quietly dark instead of pinning the API in 429 territory. An occasional one-off crash still
recovers normally — only a *sustained* loop trips the limit. To bring a limited-out
unit back after fixing the cause: `sudo systemctl reset-failed vinyl-now-playing`
then `sudo systemctl start vinyl-now-playing` (a reboot also clears it). This pairs
with the in-process absolute page cap (`_MAX_COLLECTION_PAGES`, `reader.py`) that
bounds any *single* index build; the two together mean neither a runaway build nor
a runaway restart can sit on the rate limit.

**Why the `[Unit]` ordering matters — read this before enabling.** The Raspberry
Pi has no battery-backed real-time clock, so at boot its clock is whatever
`fake-hwclock` last saved — stale, sometimes by hours or days — until
`systemd-timesyncd` reaches an NTP server *over the network*. If the app starts
before that happens, every **Last Played** date it stamps into your Discogs
collection is wrong, and the error is silent and irreversible. The ordering above
holds the service until the network is actually up (`network-online.target`,
which must be pulled in by the matching `Wants=` — ordering after it alone does
nothing) and the clock has been synchronized (`time-sync.target`).

One catch: `After=time-sync.target` only has teeth if the unit that *waits* for
the clock is enabled. With plain `systemd-timesyncd`, `time-sync.target` can be
reached **before** the clock is actually set. Enable the waiter once:

```bash
sudo systemctl enable systemd-time-wait-sync.service
```

Also set the Pi's timezone, so Last Played is stamped in your local time rather
than the default zone:

```bash
sudo raspi-config
```

Choose **Localisation Options → Timezone**. (Non-interactive equivalent, using
your own zone: `sudo timedatectl set-timezone America/Los_Angeles`.)

Confirm both took effect with `timedatectl` — it should report
`System clock synchronized: yes` and your chosen `Time zone`.

Two things to know about this gate. Enabling `systemd-time-wait-sync.service` is
system-wide — it delays *every* unit ordered after `time-sync.target`; on this
dedicated Pi that is exactly what you want. And if the Pi ever boots **offline**,
the app will not appear until the network is up and the clock syncs — a blank
screen then is the service correctly *waiting*, not a crash (`systemctl status
systemd-time-wait-sync.service` shows it still starting, blocking on the clock,
and `journalctl -u vinyl-now-playing` stays quiet rather than logging an error).
It needs the network for recognition anyway, so this costs nothing in practice.

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

### Cap the log disk usage (recommended for the SD card)

The app logs to stderr, which systemd routes into the journal. On an always-on
appliance the journal grows over time, and a rare warning storm (e.g. a cover
that repeatedly fails to decode before it is blacklisted) is buffered by
journald's own rate limiter — whose default burst (**10,000 messages / 30s**)
sits *above* the app's worst observed rate, so such a burst is written rather
than dropped and simply accumulates. On a small SD card that is worth bounding
explicitly. Set a hard size cap and tighten the rate limit:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/vinyl-now-playing.conf >/dev/null <<'EOF'
[Journal]
# Hard cap on persistent journal size (and the runtime/tmpfs journal).
SystemMaxUse=200M
RuntimeMaxUse=50M
# Tighten the burst so a repeating warning can't flood the card between
# the app's own in-code throttles.
RateLimitIntervalSec=30s
RateLimitBurst=1000
EOF
sudo systemctl restart systemd-journald
```

`SystemMaxUse` bounds the on-disk journal (journald deletes the oldest entries
once the cap is reached), so logs can never fill the card. Tune the numbers to
your card; the values above are conservative for a 16 GB+ card. This is a
belt-and-suspenders measure — the app already bounds its own repeating warnings
in code (the cover-decode blacklist), but journald is the right place to put the
absolute disk ceiling for an unattended device.

---

## 13. Optional: hide the desktop and boot straight to the app

If you want the Pi to boot directly to the vinyl display with no desktop
visible underneath:

**Auto-login to desktop** (if not already set):

```bash
sudo raspi-config
# System Options → Boot / Auto Login → Desktop Autologin
```

**Disable screen blanking** so the display stays on. On current Raspberry Pi OS
the default session is Wayland (labwc/wayfire), where the operative control is
raspi-config — **not** `xset`:

```bash
sudo raspi-config
# Display Options → Screen Blanking → No
```

> The old X11 recipe — `@xset s off / -dpms / s noblank` in
> `/etc/xdg/lxsession/LXDE-pi/autostart` — only applies if you've switched the Pi
> to an X11 session. On the default Wayland session that autostart file is never
> read and `xset` doesn't control the compositor's blanking, so it's a silent
> no-op there.

The app runs fullscreen (set in config.yaml: `display.fullscreen: true`) so the
desktop will be hidden behind it automatically once the service starts.

---

## Troubleshooting

**Display is blank / wrong resolution**
On current Raspberry Pi OS (KMS graphics) the panel's EDID usually negotiates
1024×600 on its own — check the *live* mode with `wlr-randr` (Wayland) or
`kmsprint`, not `xrandr` (which shows only an XWAYLAND virtual output). If the
mode is wrong, append `video=HDMI-A-1:1024x600M@60D` to the single line in
`/boot/firmware/cmdline.txt` and reboot (see §3). The legacy `hdmi_*` options are
ignored under KMS, and `/boot/config.txt` is a stub on Bookworm/Trixie — the real
file is `/boot/firmware/config.txt`.

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

**App starts but pygame window is invisible, or the unit lands in `failed`**
The system service can start before the autologin *session* is up and accepting
display connections — the boot-time session race. ⚠️ Adding
`After=graphical-session.target` does **nothing** here (a common suggestion): that
target exists only in the per-*user* systemd manager, and the system manager
silently ignores ordering on units it doesn't have. The unit above already
mitigates the race with `RestartSec=15` + `StartLimitBurst=10` (#201), retrying
across ~2.5 min of session bring-up instead of exhausting five tries in ~50s. If a
slow cold boot still outlasts that, either:
- recover with `sudo systemctl reset-failed vinyl-now-playing && sudo systemctl start vinyl-now-playing`, or
- make startup wait for the display socket by adding, under `[Service]`:
  `ExecStartPre=/bin/sh -c 'until [ -S /tmp/.X11-unix/X0 ]; do sleep 1; done'`
  (bounded by `TimeoutStartSec`; on the Wayland-default session the socket
  appears once Xwayland is up).
