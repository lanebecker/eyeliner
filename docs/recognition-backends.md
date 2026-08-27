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

**Cost note:** AudD bills per request. Today the app recognizes on every capture hop
(~10 s); on a paid plan you will want per-track polling (roadmap #454) to keep usage
low — until that lands, expect higher request counts on busy listening days.

## Adding another backend

`RecognizerBackend` in `src/audio/recognizer.py` is the extension point (ACRCloud is a
planned drop-in). A new backend needs a `RecognizerBackend` subclass, its name added to
`IMPLEMENTED_BACKENDS` in `src/config.py`, and a branch in
`RecognitionLoop._init_backend` — config validation and construction both check the same
set, so they can never drift.
