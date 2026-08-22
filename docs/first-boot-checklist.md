# First-Boot / Live Bring-Up Checklist — vinyl-now-playing

The unit suite is green hardware-free, but a handful of behaviors can **only** be
verified with the real Pi + UCA222 + display + live network, and a couple of
config values can **only** be tuned in your actual room. This checklist is the
thing to open the first time you power the assembled unit on. It complements
`pi-setup-guide.md` (§11 first manual run, §12 systemd autostart) and the
"What's not tested yet (requires hardware)" section of `testing-guide.md`.

Work top to bottom; each item says how to verify it and what "good" looks like.

---

## 0. Display / startup won't initialize (black screen, service won't stay up)

If the first thing you get is a black screen and `journalctl -u vinyl-now-playing`
shows the app exiting at startup, the log now names the cause on the line just
above the traceback (ARCH-10). Three failure modes:

- **"Display initialization failed — the screen will stay black."** `pygame`
  could not open a video device. Check, in order: (1) the HDMI cable is seated and
  the panel was powered on **before** the Pi booted (HDMI hot-plug is unreliable on
  the Pi); (2) a desktop / X server is actually running on the target `DISPLAY`
  (often `:0`) — the rendered systemd unit's `DISPLAY` and `XAUTHORITY` values
  must match the logged-in session; `echo $DISPLAY` in the Pi's desktop terminal
  confirms only the display value.
  ⚠️ On current Raspberry Pi OS the default session is **Wayland** (labwc/wayfire):
  `DISPLAY=:0` reaches it via Xwayland, but `/home/pi/.Xauthority` often doesn't
  exist there, so if the unit can't open the display see the "Wayland/Xwayland
  evidence required" note in `pi-setup-guide.md` §12. Keep the system service;
  point its rendered `XAUTHORITY` at the selected session-auth file. Check the
  live mode with
  `wlr-randr`, not `xrandr` (the latter shows only an XWAYLAND virtual output).
- **"Failed to construct the application components … cover_art_cache_dir … not
  writable."** The on-disk cover cache directory can't be created. Check that
  `display.cover_art_cache_dir` in `config.yaml` points at a path the service
  `User=` (default `pi`) can write — the directory itself is created automatically
  (parents and all), so the failure is a **read-only location** or a **file sitting
  where a directory must go**, not a missing folder. The default is
  `src/display/assets/cache` under the app's working directory.
- **"The 'sounddevice' audio-capture backend failed to import … `sudo apt-get
  install -y libportaudio2`."** On a fresh Pi missing `libportaudio2`, the audio
  backend can't import. As of R9-13/#396 this is caught at startup and parked as a
  friendly exit-78 `ConfigError` (the message prints the apt hint itself) instead
  of crash-looping — run `sudo apt install -y libportaudio2` (see `pi-setup-guide.md`
  §4) and `systemctl reset-failed`. This probe runs before the display and
  cover-cache checks above, so on such a Pi it is the first thing you'll see.

Because the systemd unit uses `Restart=on-failure` (bounded by
`StartLimitBurst`, STAB-4), a genuinely broken display config will retry a few
times and then drop to a `failed` state rather than loop forever — so `systemctl
status vinyl-now-playing` showing `failed`/`start-limit-hit` here means "fix the
above and `systemctl reset-failed`", not "the Pi is wedged".

## 1. Wave 1 deployability evidence (record before calling it complete)

Record values only; never paste a config value, Discogs token, or auth-file
contents into this checklist.

| Gate | Command / observation | Non-secret evidence to record | Good result |
|---|---|---|---|
| Private config (#418) | `stat -c '%a %n' /home/pi/vinyl-now-playing/config.yaml` | Path and mode | `600`; any other mode is repaired with `chmod 600`, then the app is restarted. |
| Real audio package and UCA222 (#156) | Follow the live InputStream/hot-plug procedure below after the package smoke | Package/API smoke result; selected UCA222 name/index/input channels; stream-open/audio evidence; loss/recovery journal lines; MainPID before/after; whether restart was needed; any Discogs/Last.fm side effect | The app receives audio before and after one unplug/replug with the same MainPID. |
| Display/session choice (#419) | In the logged-in graphical terminal: `printf 'DISPLAY=%s\nXAUTHORITY=%s\n' "$DISPLAY" "${XAUTHORITY:-<unset>}"`; if unset, use the read-only Xwayland `-auth` discovery in `pi-setup-guide.md` §12 | Chosen `DISPLAY`, absolute Xauthority/session-auth path, service-user readability, whether Xwayland was used, cold-boot path stability | The selected values are rendered into the system service, readable by its user, and work again after cold boot. |
| Cold boot, clock, and shutdown (#83/#201/#419) | Reboot once; inspect `timedatectl`, `systemctl status vinyl-now-playing`, and the journal; use the timed SIGTERM procedure below | Cold-boot result; `System clock synchronized` value; service status; timed SIGTERM outcome; post-stop restart/status | Clock is synchronized before startup, the service survives the graphical-session race, and SIGTERM stops cleanly within `TimeoutStopSec=30`. |
| Custom-folder Discogs probe (#366) | Follow the same-target read-only and one confirmed-write commands in `docs/testing-guide.md` | Artist/album, release ID, instance ID, custom-folder name/ID, field name, before/after values, HTTP/outcome; no token | Success disproves the hypothesis. A 404 or ambiguity is preserved as evidence with no blind retry. |

CI has already checked the installed package/API boundary and system-unit syntax;
it cannot close any of these hardware or external-state gates.

### Real InputStream and hot-plug proof (#156)

Run this only after the service is rendered and started. It does not change
configuration, but it runs the fully configured production app: recognizable
audio can trigger its normal Discogs and Last.fm behavior, including a credit on
failure/shutdown paths. Before restarting, the owner must authorize the probe and
confirm the current app state is idle and unarmed (no current tracked record or
pending completion). Start with non-recognizable audio where practical; obtain
fresh owner authorization before using recognizable music. Record any external
side effect—or the absence of one—alongside the hardware evidence.

Keep a journal window visible, then play a record long enough to produce a
`Play session started.` event: that proves the application's real
`sd.InputStream` is delivering callbacks, not merely that the device table can
be queried.

```bash
cd /home/pi/vinyl-now-playing
venv/bin/python3 -I scripts/check_audio_backend.py
sudo systemctl restart vinyl-now-playing
VNP_MAINPID_BEFORE="$(sudo systemctl show --property=MainPID --value vinyl-now-playing)"
test "$VNP_MAINPID_BEFORE" -gt 0
printf 'MainPID before unplug=%s\n' "$VNP_MAINPID_BEFORE"
sudo journalctl -u vinyl-now-playing --since '2 minutes ago' -f
```

With the journal following, confirm the initial audio event, unplug the UCA222
while capture is active, wait at least five seconds, then replug it and play
audio again. Record the capture-loss evidence (`audio stream stalled` or `Audio
capture error`), the post-replug audio event, and any `Using audio device [...]`
line. Stop the journal follow with `Ctrl+C` (do not restart the service), then
prove the process did not restart:

```bash
VNP_MAINPID_AFTER="$(sudo systemctl show --property=MainPID --value vinyl-now-playing)"
printf 'MainPID after replug=%s\n' "$VNP_MAINPID_AFTER"
test "$VNP_MAINPID_BEFORE" = "$VNP_MAINPID_AFTER"
sudo systemctl status vinyl-now-playing
```

**Good:** input callbacks produce an event both before and after replug, the
journal shows the retry/recovery path, and the MainPID is unchanged. A changed
PID, no post-replug audio event, or a private-API degradation warning is failed
hardware evidence; record it rather than claiming #156 complete.

### Timed SIGTERM proof and restart

After recording normal service behavior, measure the managed stop, inspect its
journal result, then bring the appliance back for the remaining checklist:

```bash
VNP_STOP_STARTED="$(date +%s)"
sudo systemctl stop vinyl-now-playing
VNP_STOP_FINISHED="$(date +%s)"
printf 'SIGTERM stop elapsed=%ss\n' "$((VNP_STOP_FINISHED - VNP_STOP_STARTED))"
sudo journalctl -u vinyl-now-playing --since "@$VNP_STOP_STARTED" --no-pager
sudo systemctl start vinyl-now-playing
sudo systemctl status vinyl-now-playing
```

Record the elapsed seconds and relevant shutdown journal lines. The stop must
complete within `TimeoutStopSec=30`; the final status command must show the
service started again before moving on.

## 2. Audio input is the right device

The config matches `audio.device_name` as a **case-insensitive substring** against
the device list and uses the *first* match.

- On first run, watch the startup log for:
  `Using audio device [<i>]: <name>` — confirm it's the UCA222, not a USB mic or HDMI input.
- If you see `Multiple input devices match '<name>'…`, tighten `audio.device_name`
  in `config.yaml` until only the UCA222 matches.
- If you see `Audio device '<name>' not found. Available input devices: […]`,
  copy an exact substring from that list into the config.

**Good:** one clean "Using audio device" line naming the UCA222, no multi-match warning.

## 3. Tune `audio.silence_threshold_rms` to the room (the big knob)

This is the one value that genuinely can't be set without the hardware — it's the
RMS energy line between "music" and "silence," and it depends on your turntable,
preamp gain, and room noise floor.

- Too **low**: room/needle noise reads as music, so `SESSION_ENDED` never fires →
  the now-playing card lingers and **Play Count is never credited** at side end.
- Too **high**: quiet passages/fade-outs read as silence → premature session end,
  or music never registers as started.

How to tune: play a record, then lift the needle and watch the log for the
`SilenceDetector → MUSIC_STOPPED` then `… → SESSION_ENDED` transitions (and the
display dropping to IDLE after `session_end_silence_seconds`). With the platter
spinning silently (no record), you should sit in IDLE, not flicker to LISTENING.
Nudge the threshold until both hold. Note the value you land on.

The detector uses **hysteresis** (SIL-4): music is *entered* at
`silence_threshold_rms` but only *left* once the RMS falls below **half** that
value, so an RMS hovering right at the threshold can't flap MUSIC_STARTED /
MUSIC_STOPPED every chunk. The practical consequence for tuning: your run-out /
room noise floor must sit **below half** the threshold for `SESSION_ENDED` to
fire — so set `silence_threshold_rms` to comfortably **more than 2×** your
measured noise floor (keep the floor clearly under the half-threshold line), not
just a hair above the floor. If the card lingers and never drops to IDLE at side end even
though the log shows no `MUSIC_STOPPED`, the run-out noise is sitting in the
hysteresis dead band `[½·threshold, threshold)`; raise the threshold so the dead
band clears it.

## 4. Cover-art download works over the real network (S-7 smoke test)

The SSRF-hardened, **IP-pinned HTTPS** cover fetch (resolve once → connect to the
vetted IP → TLS verified against the hostname) is unit-tested with a *mocked*
socket layer — the sandbox has no live TLS, so the real urllib3 pinned-pool path
(`server_hostname`/`assert_hostname`, certifi CA bundle) has **never run against a
real CDN**. Verify it once on the Pi.

- **End-to-end:** play a record that's in your Discogs collection and has cover
  art. The cover should appear on the display within a few seconds and the palette
  should lerp from fallback to the album's colors. That exercises
  download → decode → palette → render over the pinned path.
- **Targeted (optional), from the repo venv on the Pi:**

  ```bash
  cd ~/vinyl-now-playing        # repo root — the heredoc imports from src/
  python3 - <<'PY'
  import tempfile
  from src.display.cover_cache import CoverArtCache
  c = CoverArtCache(tempfile.mkdtemp())
  # any real Discogs or Cover Art Archive image URL the app would fetch:
  p = c.download("https://i.discogs.com/<some-real-cover>.jpg")
  print("OK ->", p, p.stat().st_size, "bytes")
  PY
  ```

  **Good:** prints a path and a non-zero byte count. A `ValueError` about host
  allow-list / non-public address / Content-Type means validation tripped (check
  the URL host is one of discogs.com / coverartarchive.org / archive.org /
  mzstatic.com); a TLS error would point at the certifi bundle on the Pi.
- If a cover fails to decode and you see it re-fetch within the track, that's the
  B-18 corrupt-file recovery working as intended.

## 5. Recognition + the churn breadcrumb

Real Shazam calls only happen on hardware.

- A dropped needle should go LISTENING → (a few chunks) → the now-playing card.
- If the display seems "stuck" not updating, check the journal for
  `Recognition churning: N consecutive unconfirmable results …` — that's the
  B-21 telemetry telling you recognition is *flipping between matches* (two
  records bleeding, room noise), not failing outright. Conservative by design; the
  log is the signal, not a bug.

## 6. Display geometry at the real resolution

The layout is resolution-independent and unit-tested across a matrix, but the
renderer's **runtime title push-down** (long track titles shrinking/wrapping in
`_compose_now_playing`) is content-dependent and only exercised live.

- Play tracks with a long title and a long album name; confirm the hero text
  shrinks/wraps without colliding the artist/album/chips or the bottom
  meta/prev-next strip, at your actual 1024×600 panel.

## 7. Full pipeline + autostart

- Let a full side play through to the end and confirm: last track detected →
  after silence, `SESSION_ENDED` → **Discogs Play Count increments** (and Last
  Played / Last.fm love if configured). Verify the increment in your Discogs
  collection.
- After the manual run looks good, enable the systemd unit (`pi-setup-guide.md`
  §12) and reboot to confirm it comes up clean on power-on.

---

## 8. R8 bring-up probes (one-time checks from the Round-8 audit; R8-19/#362)

- **Pre-flight the recognition import on the Pi's own Python** before first run:
  `python3 -c "import shazamio"`. CI now hard-imports it per matrix leg
  (R8-08/#361), but the Pi's interpreter is the one that matters — a broken
  import surfaces only as NO MATCH FOUND under a healthy-looking service.
- **Credit a record filed in a NON-default Discogs folder (R8-10/#366).** Follow
  the explicit, interactive custom-folder probe in `docs/testing-guide.md` and
  fill the evidence row above. The field write still uses virtual folder `0`; a
  404 is evidence for a separate folder-ID propagation change, not a reason to
  retry or to edit source constants.
- **One Discogs token = one rate budget** for reader AND writer: during a long
  honoured Retry-After on a credit, recognition resolves may 429 and the
  display may briefly show NO MATCH FOUND. Self-heals — don't misdiagnose it
  as two independent failures.
- **Expect a transient dip to ~20fps during the 1s palette lerp** on track
  change (measured; cosmetic, no action).

## Watch-fors / known deferrals (revisit only if observed)

- **Executor contention (issue #61, shipped — R6-32).** Blocking work is split
  across two thread pools: Discogs has its own dedicated **2-worker** pool
  (`transport.py`), and everything else — cover download, palette extract,
  scrobble, play-count/last-played/love, WAV encode — runs on an owned **8-worker**
  I/O pool (`main.py`, `_IO_EXECUTOR_MAX_WORKERS = 8`), both shut down with
  `cancel_futures` at exit. So a long Discogs 429 backoff parks at most one of the
  two Discogs workers and never blocks cover/scrobble work. **Signal to revisit:**
  cover prefetch or scrobbles feeling sluggish under a *burst* (many slow network
  calls at once) — i.e. contention *within* the 8-worker pool, no longer the old
  single-pool serialization.
- **Hue-Diversity Rule (issue #73, deferred non-feature).** The accent is the
  authentic most-saturated cover color, in isolation — no cross-album separation.
  **Signal to revisit:** back-to-back albums with similar dominant colors looking
  samey enough to bother you in person. (Implementing it trades authenticity +
  cache purity for variety — only worth it if you actually feel the lack.)
- **Integration test.** Once the above all pass, a `test_integration.py` covering
  needle-drop → identified → displayed → session-ended → Discogs-updated is the
  natural next addition (noted in `testing-guide.md`).

---

_Record the values/outcomes that matter (the tuned `silence_threshold_rms`, the
audio device index) in `config.yaml` and/or a commit message so the next bring-up
is faster._
