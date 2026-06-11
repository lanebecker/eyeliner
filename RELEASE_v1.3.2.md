# v1.3.2 — Follow-up QA Sweep

**TL;DR:** Bug-fix release. Catches one site the v1.3.1 async-loop migration
missed, fixes the dirty-flag clobber that secretly turned the pulsing
animations into freeze-frames, hardens every Discogs API call against
network hangs, and corrects several documentation inaccuracies. **210/210
tests pass.** No new features, no config changes, no breaking changes —
drop-in upgrade from v1.3.1.

---

## 🐛 Bugs fixed (4)

- **The v1.3.1 `get_event_loop()` sweep missed `src/metadata/resolver.py`.**
  The previous changelog enumerated four files swept; `resolver.py` was the
  eighth call site and should have been included. `MetadataResolver.resolve()`
  now uses `asyncio.get_running_loop()` like everywhere else.

- **The pulsing NOW PLAYING dot and IDENTIFYING spinner froze after the
  initial palette transition.** The v1.3.1 fix that set `self._dirty = True`
  inside `_render_now_playing()` / `_render_listening()` was being clobbered
  one line later by `self._dirty = False` in the run loop. Reset `_dirty`
  *before* calling `_render()` so the inner re-dirty actually survives.

- **`PlaySession.log_track` could latch a DB-only `release_id` without an
  `instance_id`.** Then `_end_session()` later called
  `increment_play_count(release_id, None)`, building a URL ending in
  `…/instances/None/fields/…` that Discogs was guaranteed to reject.
  Tightened to require BOTH IDs before latching.

- **`test_database_source_without_instance_id_does_not_increment` was a
  liar.** Named like it asserted the call was suppressed, but the body
  asserted `assert_called_once_with(12345, None)` — documenting the bug
  rather than catching it. Renamed to
  `test_database_source_without_instance_id_does_not_call_increment` and
  flipped to `assert_not_called()`.

## 🛡️ Hardening (10)

- **HTTP timeouts on every Discogs call** (`timeout=15s` on direct
  `session.get/post` calls, plus `set_timeout(connect=5, read=15)` on the
  high-level `discogs_client.Client`). No more executor threads stuck on
  hung sockets.
- **Atomic, timeout-aware cover-art download** — replaced
  `urllib.urlretrieve` with `requests.get(timeout=15, stream=True)` writing
  to a tempfile in the cache dir, then `os.replace` into place. No more
  partial files surviving a network drop or process kill.
- **Multi-device match logging** in `AudioCapture._find_device_index` —
  diagnose-able from logs if you ever plug a USB mic in next to your UCA222.
- **Case- and whitespace-insensitive recognition comparison** — Shazam
  formatting jitter no longer triggers spurious re-commits or re-scrobbles.
- **Dropped-chunk DEBUG log** in `RecognitionLoop.enqueue` — breadcrumb for
  any future "stopped identifying tracks" complaint.
- **Bounded `_palette_cache`** — 200 entries with LRU-ish eviction. No more
  unbounded growth over long Pi uptimes.
- **Mid-transition palette snap** — if a new track lands while the previous
  lerp is still in flight, the new lerp now starts from the
  currently-rendered interpolated value instead of jumping back to a stale
  base palette.
- **Adaptive render cadence** — 30 fps during palette transitions, ~10 fps
  the rest of the time. Fast enough for the 0.8s pulsing dot, much easier
  on the Pi's CPU.
- **README `venv` step** added to the Setup block (matches what
  `pi-setup-guide.md` already recommended).
- **`sync-version-badge.yml` regex** now survives hyphenated pre-release
  versions like `1.4.0-rc1`.

## 📚 Documentation fixes

- `CLAUDE.md`: corrected `discogs.token` → `discogs.user_token`, expanded
  the config snippet, updated the `PlaySession` latching description.
- `docs/architecture.md`: re-grouped `LastFmClient.love()` under
  `ListenTracker` in the system diagram (was dangling under
  `DisplayRenderer`); refreshed render-cadence and palette-cache
  descriptions; bumped the current test count to 210.
- `docs/testing-guide.md`: pytest sample output now matches the actual run.
- `docs/roadmap.md`: v1.3.2 section added, v1.3.1 demoted from "current".

## 🧪 Tests

**208 → 210.** Added two model-level regression tests for the new latching
rule:

- `test_log_track_does_not_latch_database_source_without_instance_id`
- `test_log_track_database_then_collection_latches_collection_only`

```
$ pytest -q
210 passed in 0.34s
```

## ⬆️ Upgrade instructions

Drop-in from v1.3.1 — no config changes required:

```bash
cd ~/vinyl-now-playing
git pull
source venv/bin/activate
pip install -r requirements.txt   # picks up any updated minimum versions
sudo systemctl restart vinyl-now-playing
```

If you have the GitHub Actions workflow enabled, the version badge will
auto-update on the next push of `VERSION` (already 1.3.2 in this release).

---

**Full diff:** [`v1.3.1...v1.3.2`](https://github.com/lanebecker/vinyl-now-playing/compare/v1.3.1...v1.3.2)

**Commits:**
- [`be9cc2b`](https://github.com/lanebecker/vinyl-now-playing/commit/be9cc2b45a84ca9e0641f4ce50a8fee2a7cf81e2) — VERSION, README, requirements
- [`d94d0ad`](https://github.com/lanebecker/vinyl-now-playing/commit/d94d0ad5e642b1eb62d1b76fc6101a4c30654e7d) — CLAUDE.md + small/medium source files
- [`8cc3532`](https://github.com/lanebecker/vinyl-now-playing/commit/8cc353214cf29c77401033837b9f85567bae349f) — CHANGELOG, roadmap, test_listen_tracker
- [`3cac9d4`](https://github.com/lanebecker/vinyl-now-playing/commit/3cac9d420c77531e2eeb62a0503429e8daac1eb0) — discogs_client timeouts + test_models regression tests
- [`8069272`](https://github.com/lanebecker/vinyl-now-playing/commit/806927210e4c2bc785d2c2e91f3f18778574f1ac) — renderer hardening + final doc updates
