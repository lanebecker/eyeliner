# First-Boot / Live Bring-Up Checklist — vinyl-now-playing

The unit suite is green hardware-free, but a handful of behaviors can **only** be
verified with the real Pi + UCA222 + display + live network, and a couple of
config values can **only** be tuned in your actual room. This checklist is the
thing to open the first time you power the assembled unit on. It complements
`pi-setup-guide.md` (§11 first manual run, §12 systemd autostart) and the
"What's not tested yet (requires hardware)" section of `testing-guide.md`.

Work top to bottom; each item says how to verify it and what "good" looks like.

---

## 1. Audio input is the right device

The config matches `audio.device_name` as a **case-insensitive substring** against
the device list and uses the *first* match.

- On first run, watch the startup log for:
  `Using audio device [<i>]: <name>` — confirm it's the UCA222, not a USB mic or HDMI input.
- If you see `Multiple input devices match '<name>'…`, tighten `audio.device_name`
  in `config.yaml` until only the UCA222 matches.
- If you see `Audio device '<name>' not found. Available input devices: […]`,
  copy an exact substring from that list into the config.

**Good:** one clean "Using audio device" line naming the UCA222, no multi-match warning.

## 2. Tune `audio.silence_threshold_rms` to the room (the big knob)

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

## 3. Cover-art download works over the real network (S-7 smoke test)

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

  ```python
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

## 4. Recognition + the churn breadcrumb

Real Shazam calls only happen on hardware.

- A dropped needle should go LISTENING → (a few chunks) → the now-playing card.
- If the display seems "stuck" not updating, check the journal for
  `Recognition churning: N consecutive unconfirmable results …` — that's the
  B-21 telemetry telling you recognition is *flipping between matches* (two
  records bleeding, room noise), not failing outright. Conservative by design; the
  log is the signal, not a bug.

## 5. Display geometry at the real resolution

The layout is resolution-independent and unit-tested across a matrix, but the
renderer's **runtime title push-down** (long track titles shrinking/wrapping in
`_compose_now_playing`) is content-dependent and only exercised live.

- Play tracks with a long title and a long album name; confirm the hero text
  shrinks/wraps without colliding the artist/album/chips or the bottom
  meta/prev-next strip, at your actual 1024×600 panel.

## 6. Full pipeline + autostart

- Let a full side play through to the end and confirm: last track detected →
  after silence, `SESSION_ENDED` → **Discogs Play Count increments** (and Last
  Played / Last.fm love if configured). Verify the increment in your Discogs
  collection.
- After the manual run looks good, enable the systemd unit (`pi-setup-guide.md`
  §12) and reboot to confirm it comes up clean on power-on.

---

## Watch-fors / known deferrals (revisit only if observed)

- **Executor contention (issue #61, deferred).** All blocking work (cover
  download, palette extract, 3× Discogs, scrobble, play-count/last-played/love,
  WAV encode) shares one default thread pool. It's deferred on purpose — the
  consumers are bursty, mostly serialized, and I/O-bound. **Signal to revisit:**
  cover prefetch or scrobbles feeling sluggish *during* a Discogs rate-limit (429)
  event. Until then, the 10s 429 backoff cap is the proportionate mitigation.
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
