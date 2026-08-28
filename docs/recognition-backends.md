# Recognition backends

eyeliner identifies the playing track through a pluggable **recognition backend**,
selected with `recognition.backend` in `config.yaml`. Two are implemented:

| Backend | Cost | Setup | Reliability |
|---|---|---|---|
| **`shazamio`** (default) | Free | none | Unofficial / reverse-engineered — can drift out of sync with Shazam's servers and miss tracks it matched before. Best on popular music. |
| **`audd`** (recommended) | Paid (free trial; ~$5 / 1000 requests after) | an API token | Commercial, maintained service; markedly more reliable in practice. |

The default is `shazamio` so the app runs on a fresh clone with no signup. **For a
set-and-forget appliance, AudD is recommended.**

## ShazamIO (default, free)

```yaml
recognition:
  backend: "shazamio"
```

No account needed. **Caveat:** ShazamIO is an unofficial, reverse-engineered Shazam
client with no stability guarantee. When Shazam changes something server-side it can
start returning no-match for audio it identified before ("worked yesterday, broken
today"), and it tends to miss deeper, less-popular tracks. If recognition gets flaky,
switch to AudD.

## AudD (recommended)

1. Get an API token at <https://audd.io> (free trial; paid plans after).
2. Configure:

```yaml
recognition:
  backend: "audd"
  audd:
    api_token: "YOUR_AUDD_TOKEN"
```

The token is a secret — keep `config.yaml` mode `600` and never commit it (it is
gitignored). Startup **fails fast** with a clear error if `backend: audd` is set
without a token.

**Cost note:** AudD bills per request. As of v1.6.0, recognition runs **~once per
track** — it identifies a track, then idles until the predicted next-track boundary
(Discogs track duration minus the AudD match offset), so a normal listening month
stays well inside a small quota. A track with no Discogs duration falls back to a
re-check every `recognition.max_idle_recheck_seconds` (default 240 s), which also
caps the idle between tracks so a bad duration never freezes the display.

**ShazamIO note:** ShazamIO reports no match offset, so boundary prediction is less
precise on it (it idles a full duration and may reactivate a little late); AudD
reports the offset and lands the boundary accurately. Either way the re-check cap
bounds the drift.

## Adding another backend

`RecognizerBackend` in `src/audio/recognizer.py` is the extension point (ACRCloud is a
planned drop-in). A new backend needs a `RecognizerBackend` subclass, its name added to
`IMPLEMENTED_BACKENDS` in `src/config.py`, and a branch in
`RecognitionLoop._init_backend` — config validation and construction both check the same
set, so they can never drift.

## Debugging recognition (track skips / mis-timing)

Set `EYELINER_DEBUG_RECOGNITION=1` to log, at INFO, two diagnostics per side: each
backend poll (`recognition-debug poll: result=… off=… status=… cur=… pending=… xN`)
and each predicted next-track boundary (`recognition-debug idle: dur=… off=… wait=…`).
Off by default — normal runs emit nothing. It's the built-in replacement for
hand-patching debug lines; use it to see whether a skipped track was a boundary
overshoot (the `idle` wait landed past the track) or the backend simply not
returning that track (`poll result=None` throughout it).

**Manual run** (foreground, headless — audio + recognition still work over SSH):

```bash
systemctl --user stop eyeliner.service
cd ~/eyeliner && SDL_VIDEODRIVER=dummy EYELINER_DEBUG_RECOGNITION=1 \
  venv/bin/python main.py 2>&1 | tee ~/eyeliner-debug.log
# ...play the side, Ctrl+C...
systemctl --user restart eyeliner.service
grep -E 'recognition-debug|Track confirmed|churning|NO MATCH' ~/eyeliner-debug.log
```

**Autostart service** (to capture without a manual run): add to the user unit
`~/.config/systemd/user/eyeliner.service`, under `[Service]`:

```ini
Environment=EYELINER_DEBUG_RECOGNITION=1
StandardOutput=append:%h/eyeliner-debug.log
StandardError=append:%h/eyeliner-debug.log
```

then `systemctl --user daemon-reload && systemctl --user restart eyeliner.service`.
The explicit logfile is the reliable path because `journalctl --user` can come back
empty when the user journal isn't persisted (default on some Pi OS images). Remove
those three lines (and `daemon-reload` + restart) to turn diagnostics back off.
