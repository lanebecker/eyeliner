# Wave 2 Metadata Recovery and Cache Coherence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Apply superpowers:test-driven-development to every behavior change and superpowers:verification-before-completion before any pass claim. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Discogs cache state truthful after failed/cooldown-skipped refreshes and safely recover one completed-play credit when a formerly valid collection instance has definitively been replaced.

**Architecture:** Reader-owned explicit refresh outcomes distinguish owned, clean-no-match, and cooldown-skip states while strict pagination and a separate failed-build backoff preserve both truth and request bounds. A strictly validated writer read carries snapshot-safe replacement-instance evidence through a narrow tracker callback to a serialized resolver recovery path; only the same release with exactly one proven new instance can receive one replacement credit attempt.

**Tech Stack:** Python 3.11–3.13, asyncio, dataclasses/enums, pytest/pytest-asyncio, Discogs REST API through the existing `DiscogsHttp` transport.

**Spec:** `docs/superpowers/specs/2026-08-22-wave2-metadata-recovery-design.md`

## Global Constraints

- Work only in `/private/tmp/vnp-wave2-metadata-recovery` on branch `codex/r10-wave2-metadata-recovery`, based on merged Wave 1 `origin/main` SHA `7915dcacea00dea7846b3d0dfa4b915ec6f74dbe` plus the approved design commits.
- `DESIGN.md` is noncanonical. Owner decisions, `docs/decisions/remediation-guardrails.md`, live issues #420/#421, ratified production/tests, and closure rationale control.
- Implement #420 completely and review it before starting #421.
- Preserve the #191 15-minute speculative cooldown, stamp-before-I/O behavior, and request-rate protection. Recovery does not consume, reset, or extend that cooldown.
- Preserve #242 swap-on-success. Never promote or cache a partial collection index as complete ownership truth.
- Preserve immortal positive collection entries on ordinary reads. Only a definitive writer observation may invalidate the exact stale entry.
- Preserve #186 read-once/absolute-set behavior and #229 bounded rate-limit handling. Identity recovery occurs before an old absolute target exists and has one budget.
- Preserve META-7 Last Played independence once a safe identity exists; Last Played remains one attempt.
- Do not persist the collection index or instance IDs, add a positive TTL, add background synchronization, or perform a live Discogs write.
- Never use `git add -A`; stage only files named by the active task.
- Use the canonical clone's development interpreter only as `/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python`. It is Python 3.9 and is a fast local signal only; final acceptance requires GitHub Python 3.11/3.12/3.13.
- The pre-change focused Wave 2 baseline is 201 passing tests. Record every RED failure and fresh GREEN result in the task report.

## File and interface map

| Task | Files owned | Interface produced |
|---|---|---|
| 1 | `src/metadata/discogs/outcomes.py`, `reader.py`, `resolver.py`, #420 cache/index tests | `CollectionRefreshResult` makes ownership/cacheability explicit; strict index builds back off without partial promotion. |
| 2 | `outcomes.py`, `writer.py`, `listen_tracker.py`, writer/tracker tests | `PlayCountReadResult` distinguishes ready, definitive missing with observed IDs, and ordinary abort without changing write safety. |
| 3 | `models.py`, `resolver.py`, `reader.py`, resolver/model tests | Latched `album_resolve_key`, one resolver reader gate, `recover_collection_instance(...)`, and same-release/singleton cache recovery. |
| 4 | `listen_tracker.py`, `main.py`, tracker/idempotency/concurrency/wiring tests | One pre-plan recovery attempt, recovered write identity, preserved META-7, and composition-root injection. |
| 5 | dedicated acceptance matrix plus durable docs | Cross-layer #420/#421 reproductions, regression contract, changelog, and issue-ready evidence. |
| 6 | all Wave 2 files | Fresh integrated/full verification, adversarial review, supported CI, and GitHub closeout. |

---

### Task 1: Make refresh and index truth explicit (#420)

**Files:**
- Create: `src/metadata/discogs/outcomes.py`
- Modify: `src/metadata/discogs/reader.py` around `_get_collection_index` and `refresh_index_and_research`
- Modify: `src/metadata/resolver.py` around `resolve`
- Modify: `tests/test_cache_expiry.py`
- Modify: `tests/test_resolver_error_no_cache.py`
- Modify: `tests/test_discogs_collection_index.py`
- Modify: `tests/test_resolver.py` only where refresh mocks require the new type
- Modify: `tests/test_reader_efficiency_w6.py`

**Interfaces:**
- Produces: `CollectionRefreshState`, `CollectionRefreshResult`, strict complete-index replacement, build-failure backoff, and explicit resolver cache branching.
- Preserves: `DiscogsReader.search_collection(...) -> Optional[dict]`, collection index TTL, global speculative cooldown, and ordinary positive cache behavior.

- [ ] **Step 1: Write RED tests for the tri-state refresh contract**

In `tests/test_cache_expiry.py`, migrate `_page()` to return complete pagination:

```python
def _page(releases, page=1, pages=1, per_page=100, items=None):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "releases": releases,
        "pagination": {
            "page": page,
            "pages": pages,
            "per_page": per_page,
            "items": len(releases) if items is None else items,
        },
    }
    return resp
```

Add tests named:

- `test_refresh_returns_owned_after_complete_rebuild`
- `test_refresh_returns_clean_no_match_after_complete_rebuild`
- `test_refresh_returns_cooldown_skipped_after_successful_rebuild`
- `test_failed_refresh_marks_cooldown_provenance_unknown`
- `test_cooldown_skip_after_failed_refresh_does_not_repage`
- `test_resolver_caches_database_after_clean_no_match`
- `test_resolver_caches_database_on_cooldown_after_proven_success`
- `test_resolver_does_not_cache_database_on_cooldown_after_failed_refresh`

Assert explicit state and provenance, not truthiness:

```python
assert outcome.state is CollectionRefreshState.COOLDOWN_SKIPPED
assert outcome.cooldown_follows_successful_rebuild is False
assert len(resolver._album_cache) == 0
```

- [ ] **Step 2: Add the executed #420 failure-then-skip RED reproduction**

In `tests/test_resolver_error_no_cache.py`, run two resolves through one reader state. The first forced refresh raises after stamping its attempt; the second resolve occurs inside the cooldown and returns `COOLDOWN_SKIPPED` with unsuccessful provenance. Both return database metadata for display and both leave `_album_cache` empty. Assert the second resolve adds no collection-page request.

Run:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_cache_expiry.py tests/test_resolver_error_no_cache.py
```

Expected RED: current `None` return has no state/provenance, and the second resolve stores one database downgrade.

- [ ] **Step 3: Rewrite the conflicting STAB-4 test RED-first**

Replace `test_stab4_paging_stops_at_absolute_cap_with_partial_index` in `tests/test_discogs_collection_index.py`. The old test deliberately requires caching partial truth and conflicts with the approved Wave 2 contract. Add:

- `test_page_cap_rejects_partial_index_and_preserves_prior_complete_snapshot`
- `test_page_cap_without_prior_snapshot_leaves_index_unavailable`
- `test_failed_build_backoff_prevents_second_page_walk_with_prior_snapshot`
- `test_failed_build_backoff_prevents_second_page_walk_without_snapshot`
- `test_build_backoff_serves_positive_from_prior_snapshot`
- `test_build_backoff_miss_is_unknown_not_clean`
- `test_build_backoff_refusal_does_not_change_speculative_cooldown_state`
- `test_successful_complete_rebuild_clears_failure_backoff`
- parameterized `test_incomplete_pagination_never_promotes_candidate_index`

The parameter matrix covers missing pagination, wrong page, changing page count, inconsistent `items`, invalid `per_page`, malformed release/instance IDs, premature empty page, and advertised pages beyond the cap. A second lookup inside backoff must not add an HTTP request.

Expected RED: current code caches the capped partial index and immediately retries other failed builds.

- [ ] **Step 4: Add the explicit refresh types**

Create `src/metadata/discogs/outcomes.py` with:

```python
from dataclasses import dataclass
from enum import Enum, auto
from types import MappingProxyType
from typing import Any, Mapping, Optional


class CollectionRefreshState(Enum):
    OWNED = auto()
    CLEAN_NO_MATCH = auto()
    COOLDOWN_SKIPPED = auto()


@dataclass(frozen=True)
class CollectionRefreshResult:
    state: CollectionRefreshState
    result: Optional[Mapping[str, Any]] = None
    cooldown_follows_successful_rebuild: bool = False

    def __post_init__(self):
        if (self.state is CollectionRefreshState.OWNED) != (self.result is not None):
            raise ValueError("OWNED requires a result; other states forbid one")
        if (self.state is not CollectionRefreshState.COOLDOWN_SKIPPED
                and self.cooldown_follows_successful_rebuild):
            raise ValueError("cooldown provenance is valid only for a skipped refresh")
        if self.result is not None:
            object.__setattr__(self, "result", MappingProxyType(dict(self.result)))
```

The frozen envelope and defensive read-only mapping make state/provenance and
the top-level payload immutable. Nested enrichment lists retain their existing
copy-on-`TrackMetadata` boundary. The resolver converts the mapping to its own
`dict` before cache storage. Do not add later-task types yet.

- [ ] **Step 5: Implement strict index replacement and build backoff**

In `reader.py`, add a separate 15-minute `_COLLECTION_BUILD_FAILURE_BACKOFF_SECONDS` and monotonic failure stamp. Extract one strict builder that constructs a candidate locally and assigns `_collection_index`/`_collection_index_built_at` only after every advertised page validates and completes.

The validator must require exact positive integers (`type(value) is int`, excluding booleans) for page counts, release IDs, and instance IDs; stable `pages`, `per_page`, and total `items` across the walk; current `pagination.page`; and completion below `_MAX_COLLECTION_PAGES`. Count raw release rows separately from collapsed index keys: every nonfinal page contains exactly `per_page` rows, the final page contains at most `per_page`, and the accumulated raw row count equals `items`. Duplicate copies of one release remain valid rows even though the ordinary index retains one entry. On any failure:

```python
self._last_collection_build_failure_at = time.monotonic()
# keep the prior complete self._collection_index and built_at unchanged
raise CollectionIndexIncomplete("collection pagination was incomplete")
```

Add a private frozen access envelope:

```python
@dataclass(frozen=True)
class _CollectionIndexView:
    index: dict
    misses_are_authoritative: bool
```

`_get_collection_index()` returns an authoritative view for an injected/fresh
or newly completed index. During build backoff it returns a prior complete
snapshot with `misses_are_authoritative=False`; with no prior snapshot it raises
unknown immediately. `search_collection()` may return a positive match from a
stale view, but after both matching strategies miss it raises
`CollectionOwnershipUnknown` when the view is non-authoritative. This explicit
signal prevents a stale miss from becoming clean `None`. Successful completion
clears the failure stamp.

Migrate successful page fixtures in both `tests/test_discogs_collection_index.py`
and `tests/test_reader_efficiency_w6.py` to complete pagination metadata. Add a
multi-page duplicate-release fixture proving raw `items` equals accumulated
rows while `len(index)` is smaller because duplicate releases collapse.

- [ ] **Step 6: Implement refresh outcome and provenance**

Keep `_last_index_refresh_at` and add `_last_speculative_refresh_succeeded`. At the start of a real speculative attempt, stamp time and set success false. After a strict full rebuild completes, set it true and return:

```python
if match is not None:
    return CollectionRefreshResult(CollectionRefreshState.OWNED, result=match)
return CollectionRefreshResult(CollectionRefreshState.CLEAN_NO_MATCH)
```

Inside the existing cooldown, return `COOLDOWN_SKIPPED` with the stored provenance and do no I/O. A build-backoff refusal raises as unknown without altering the speculative timestamp. Preserve the old index/built-at on every exception.

- [ ] **Step 7: Branch explicitly in the resolver**

Replace `if upgraded:` with a state switch. `OWNED` stores the positive result. `CLEAN_NO_MATCH` keeps `discogs_completed=True`. `COOLDOWN_SKIPPED` keeps it true only when `cooldown_follows_successful_rebuild` is true; otherwise set it false so the display may use database metadata but `_album_cache` stays empty. Exceptions retain the existing no-cache behavior and error taxonomy.

Migrate every #420 fixture returning bare `None` to an explicit clean or skipped result; do not let a default `MagicMock` accidentally exercise the owned branch.

- [ ] **Step 8: Verify #420 GREEN and preserved behavior**

Run:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_cache_expiry.py tests/test_resolver_error_no_cache.py tests/test_discogs_collection_index.py tests/test_resolver.py tests/test_reader_efficiency_w6.py
git diff --check
```

Require all new tests plus `test_collection_cache_never_expires`, swap-on-success, cooldown request-count, and reader-efficiency tests to pass.

- [ ] **Step 9: Independent #420 review and commit**

Reviewer attacks: a failed refresh followed by another album; missing/malformed pagination; page-cap retry loops; cooldown reset on error; stale index loss; `bool` IDs; and ordinary positive-cache expiry. Resolve findings, rerun Step 8, then commit only Task 1 files:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
git add src/metadata/discogs/outcomes.py src/metadata/discogs/reader.py src/metadata/resolver.py tests/test_cache_expiry.py tests/test_resolver_error_no_cache.py tests/test_discogs_collection_index.py tests/test_resolver.py tests/test_reader_efficiency_w6.py
git commit -m 'fix: keep Discogs refresh cache state truthful'
```

---

### Task 2: Expose definitive missing-instance evidence at the writer boundary (#421)

**Files:**
- Modify: `src/metadata/discogs/outcomes.py`
- Modify: `src/metadata/discogs/writer.py`
- Modify: `src/tracking/listen_tracker.py` only to consume typed ready/abort results safely before recovery exists
- Modify: `tests/test_discogs_client.py`
- Modify: `tests/test_discogs_client_robustness.py`
- Modify: `tests/test_credit_idempotent_186.py`
- Modify: `tests/test_listen_tracker.py` fixtures for typed reads

**Interfaces:**
- Produces: `PlayCountReadState` and immutable `PlayCountReadResult`; definitive missing carries `observed_instance_ids: tuple[int, ...]`.
- Preserves: `increment_play_count(...) -> bool`, absolute `set_play_count`, current retry-on-429 behavior, and no write on every ambiguous read.

- [ ] **Step 1: Write typed-read RED tests**

In `tests/test_discogs_client.py`, add a helper whose complete single-page body includes `pagination.page/pages/items/per_page`, each item's `instance_id`, `folder_id`, and `basic_information.id` equal to the requested release.

Add:

- `test_read_play_count_returns_ready_with_integer_count`
- `test_read_play_count_returns_ready_for_confirmed_blank`
- `test_read_play_count_definitively_missing_only_from_valid_complete_single_page`
- `test_read_play_count_definitively_missing_with_empty_observed_tuple`
- `test_found_expected_instance_is_ready_not_missing`
- `test_noninteger_count_is_abort_not_missing`
- parameterized `test_missing_instance_evidence_remains_abort`

The abort matrix includes missing pagination, `pages > 1`, inconsistent item count, invalid/too-small `per_page`, duplicate instance IDs, malformed IDs including booleans, wrong release IDs, malformed JSON, every non-200 including 404, and transport exceptions. Assert only definitive missing exposes the exact sorted/response-order validated tuple, and ambiguous results expose no IDs.

Run:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_discogs_client.py
```

Expected RED: `read_play_count` returns tuple/`None` and cannot distinguish validated absence.

- [ ] **Step 2: Add the writer result types**

Extend `outcomes.py`:

```python
class PlayCountReadState(Enum):
    READY = auto()
    DEFINITIVE_INSTANCE_MISSING = auto()
    ABORT = auto()


@dataclass(frozen=True)
class PlayCountReadResult:
    state: PlayCountReadState
    field_id: Optional[int] = None
    current_count: Optional[int] = None
    observed_instance_ids: tuple[int, ...] = ()

    def __post_init__(self):
        ready = self.state is PlayCountReadState.READY
        missing = self.state is PlayCountReadState.DEFINITIVE_INSTANCE_MISSING
        if ready:
            if type(self.field_id) is not int or self.field_id <= 0:
                raise ValueError("READY requires a positive integer field ID")
            if type(self.current_count) is not int or self.current_count < 0:
                raise ValueError("READY requires a nonnegative integer count")
            if self.observed_instance_ids:
                raise ValueError("READY cannot carry replacement instances")
        elif self.field_id is not None or self.current_count is not None:
            raise ValueError("non-READY results cannot carry field/count data")
        if missing:
            if (len(set(self.observed_instance_ids)) != len(self.observed_instance_ids)
                    or any(type(value) is not int or value <= 0
                           for value in self.observed_instance_ids)):
                raise ValueError("MISSING instances must be unique positive integers")
        elif self.observed_instance_ids:
            raise ValueError("only MISSING can carry replacement instances")
```

ABORT has no payload by construction. A MISSING result may carry an empty tuple
when a complete validated response proves the release currently has zero
instances; state, not tuple truthiness, distinguishes it from ABORT.

- [ ] **Step 3: Implement snapshot-safe writer classification**

Refactor the private read helper to return a private structured classification. If the expected instance is found, return its field value through the existing confirmed-blank/noninteger rules; a found target does not require absence proof. Only when the target is absent, validate the entire one-page response exactly as the spec requires. Return definitive missing with all observed IDs only after that validation; otherwise ABORT.

Do not treat HTTP 404 as definitive. Do not follow `pages > 1`. Do not log response bodies or note values. `read_play_count` converts the private classification into `PlayCountReadResult`.

- [ ] **Step 4: Preserve convenience and tracker behavior during migration**

Update `increment_play_count` to act only on READY. In `ListenTracker`, adapt the current credit attempt to read `state.field_id/current_count`; ABORT remains retryable as before. Define the terminal signal exactly:

```python
class _DefinitiveMissingInstance(Exception):
    def __init__(self, result: PlayCountReadResult):
        super().__init__("collection instance is definitively missing")
        self.result = result
```

When the stale read returns MISSING, `_credit_attempt` raises this signal.
`_finalize_write_with_retry` adds `except _DefinitiveMissingInstance: raise`
before its broad `except Exception`, so it performs no retry or backoff.
`_credit_completed_album` catches it outside the helper; until Task 4 adds
recovery, it records Play Count failure and suppresses Last Played because the
target is known stale. ABORT remains normally retryable and retains META-7.
Task 4 reuses the exception's carried result to invoke recovery.

Update `make_writer_mock()` in `tests/test_listen_tracker.py` to return `PlayCountReadResult(PlayCountReadState.READY, 3, 0)` while preserving its existing `increment_play_count` delegation assertions. Add a Task 2 test proving MISSING performs exactly one old-instance GET, zero field POSTs, zero Last Played calls, and no retry sleep.

- [ ] **Step 5: Verify writer GREEN and #186 preservation**

Run:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_discogs_client.py tests/test_discogs_client_robustness.py tests/test_credit_idempotent_186.py tests/test_listen_tracker.py
git diff --check
```

Require all existing META-1/META-2, username encoding, 429, blank-field, noninteger, and ambiguous-applied-POST tests to pass.

- [ ] **Step 6: Independent writer-boundary review and commit**

Reviewer attempts to promote 404, malformed pagination, multiple pages, duplicate IDs, boolean IDs, wrong releases, and missing keys to MISSING; attempts to leak observed IDs from ABORT; and verifies no stale POST occurs. Resolve findings and commit explicit files:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
git add src/metadata/discogs/outcomes.py src/metadata/discogs/writer.py src/tracking/listen_tracker.py tests/test_discogs_client.py tests/test_discogs_client_robustness.py tests/test_credit_idempotent_186.py tests/test_listen_tracker.py
git commit -m 'fix: classify definitive Discogs instance removal'
```

---

### Task 3: Add serialized resolver recovery and latched identity (#421)

**Files:**
- Modify: `src/metadata/discogs/outcomes.py`
- Modify: `src/metadata/models.py`
- Modify: `src/metadata/discogs/reader.py`
- Modify: `src/metadata/resolver.py`
- Modify: `tests/test_models.py`
- Modify: `tests/test_resolver.py`
- Modify fixture-only resolver construction in `tests/test_cache_expiry.py`, `tests/test_resolver_error_no_cache.py`, `tests/test_coverart_timeout_238.py`, and `tests/test_tracklist_neighbours.py`

**Interfaces:**
- Produces: `CollectionIdentity`, `PlaySession.album_resolve_key`, `MetadataResolver.recover_collection_instance(resolve_key, expected_release_id, expected_instance_id, observed_instance_ids)`, and one reader gate shared by ordinary resolve and recovery.
- Consumes: Task 1 strict rebuild/backoff and Task 2 validated observed instance tuple.

- [ ] **Step 1: Write RED model-latching tests**

Add to `tests/test_models.py`:

- `test_log_track_latches_resolve_key_with_first_collection_identity`
- `test_latched_album_resolve_key_does_not_follow_later_track`
- `test_database_track_does_not_latch_album_resolve_key`

The first collection track must atomically latch release ID, instance ID, and its exact `resolve_key`. Later tracks may update `last_release_resolve_key` but not the album latch.

- [ ] **Step 2: Write RED resolver recovery tests**

Add to `tests/test_resolver.py`:

- `test_recovery_invalidates_exact_stale_positive_and_returns_same_release_new_instance`
- `test_recovery_allows_absent_cache_entry_after_lru_eviction`
- `test_recovery_does_not_erase_newer_nonmatching_cache_entry`
- `test_recovery_accepts_newer_cache_only_when_it_matches_proven_singleton`
- `test_recovery_refuses_same_instance`
- `test_recovery_refuses_different_release`
- `test_recovery_refuses_zero_or_multiple_replacement_instances`
- `test_recovery_empty_enumeration_invalidates_stale_entry_and_refuses`
- `test_duplicate_instances_leave_album_key_uncached`
- `test_recovery_failure_leaves_known_stale_album_entry_invalidated`
- `test_recovery_during_failed_speculative_cooldown_does_not_change_cooldown_state`

Use exact cache tuples `(MetadataSource.DISCOGS_COLLECTION, payload, stored_at)` and assert the expected stale entry alone is removed. For the success case, the reader may return a collapsed instance, but the cached payload and returned identity must use the writer-proven singleton.

- [ ] **Step 3: Write RED serialization tests**

Add:

- `test_concurrent_cache_misses_serialize_reader_sequences`
- `test_waiting_resolve_rechecks_cache_after_acquiring_reader_gate`
- `test_collection_cache_fast_path_does_not_enter_reader_gate`
- `test_ordinary_resolve_and_recovery_are_serialized_by_one_gate`

Use `asyncio.Event` barriers in mocked `reader.run` calls. Assert the maximum concurrent reader sequence is one, a waiting resolve consumes the newly populated cache instead of re-reading, and the immortal positive fast path does not wait behind recovery.

Run:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_models.py tests/test_resolver.py
```

Expected RED: no latched key, recovery method, or resolver gate exists.

- [ ] **Step 4: Add identity type and session latch**

Add to `outcomes.py`:

```python
@dataclass(frozen=True)
class CollectionIdentity:
    release_id: int
    instance_id: int

    def __post_init__(self):
        if type(self.release_id) is not int or self.release_id <= 0:
            raise ValueError("release_id must be a positive integer")
        if type(self.instance_id) is not int or self.instance_id <= 0:
            raise ValueError("instance_id must be a positive integer")
```

Add `album_resolve_key: Optional[tuple[str, str]] = None` beside the album IDs in `PlaySession` and set it in the same conditional block that latches those IDs.

- [ ] **Step 5: Add one resolver-owned reader gate**

Initialize `self._reader_gate = asyncio.Lock()`. Keep the pre-lock cache fast path, then acquire and recheck the cache before running the whole mutable reader sequence. Extract:

```python
async def _resolve_discogs_locked(
    self, raw: "RawRecognitionResult", key: tuple[str, str]
) -> tuple[Optional[TrackMetadata], bool]:
    """Return (resolved metadata or None, whether Discogs completed cleanly)."""
```

Move the existing collection/database/speculative-refresh sequence into that method. `resolve()` releases the reader gate before invoking cover-art fallback and uses the returned boolean for the existing fallback-cache decision. Do not treat the executor as a lock and do not hold tracker lifecycle locks across this gate.

Update every `MetadataResolver.__new__` test fixture to install `asyncio.Lock()` explicitly.

- [ ] **Step 6: Add recovery rebuild without cooldown mutation**

Add a reader method that strictly rebuilds collection state and re-runs normal album matching while bypassing only `_last_index_refresh_at`. It respects the separate build-failure backoff and swap-on-success and leaves `_last_speculative_refresh_succeeded` unchanged. It returns the ordinary validated release-level match; it does not claim instance multiplicity.

- [ ] **Step 7: Implement conditional resolver recovery**

Implement:

```python
async def recover_collection_instance(
    self,
    resolve_key: tuple[str, str],
    expected_release_id: int,
    expected_instance_id: int,
    observed_instance_ids: tuple[int, ...],
) -> Optional[CollectionIdentity]:
```

Validate the resolve key, stale IDs, and that every observed tuple member is a
unique exact positive integer, but do not reject tuple cardinality yet. Under
`_reader_gate`, inspect without erasing unrelated/newer cache state. Pop only
the exact stale collection pair; absent is allowed. A newer entry is accepted
only if the tuple contains exactly one replacement and its exact pair equals
the expected release plus that singleton.

After the exact stale entry has been removed, require cardinality one and a
candidate different from the stale instance. Empty, multiple, or same-instance
evidence returns `None` immediately without rebuilding or repopulating the key.
This order ensures safe refusal does not leave the immortal stale entry intact.

After a strict recovery rebuild, require one unambiguous match whose release ID equals `expected_release_id`. Copy its payload, overwrite `instance_id` with the proven singleton, store that positive entry, and return `CollectionIdentity`. On different/no/ambiguous match or error, leave the key uncached and return `None` after actionable non-secret logging.

- [ ] **Step 8: Verify resolver/model GREEN**

Run:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_models.py tests/test_resolver.py tests/test_cache_expiry.py tests/test_resolver_error_no_cache.py tests/test_coverart_timeout_238.py tests/test_tracklist_neighbours.py tests/test_discogs_collection_index.py
git diff --check
```

- [ ] **Step 9: Independent cache/concurrency review and commit**

Reviewer attacks: stale-pop-after-await, LRU eviction, newer cache replacement, duplicate tuple, bool IDs, changed release, collapsed reader instance, failed recovery during active cooldown, two concurrent reader sequences, and a cache fast path blocked by I/O. Resolve findings and commit explicit files.

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
git add src/metadata/discogs/outcomes.py src/metadata/models.py src/metadata/discogs/reader.py src/metadata/resolver.py tests/test_models.py tests/test_resolver.py tests/test_cache_expiry.py tests/test_resolver_error_no_cache.py tests/test_coverart_timeout_238.py tests/test_tracklist_neighbours.py
git commit -m 'feat: recover stale Discogs collection identity safely'
```

---

### Task 4: Wire one recovered credit attempt through the tracker (#421)

**Files:**
- Modify: `src/tracking/listen_tracker.py`
- Modify: `main.py`
- Modify: `tests/test_listen_tracker.py`
- Modify: `tests/test_credit_idempotent_186.py`
- Modify: `tests/test_finalize_retry_after.py`
- Modify: `tests/test_listen_tracker_conc2.py`
- Modify: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: Task 2 `PlayCountReadResult` and Task 3 recovery callback/latched key.
- Produces: `ListenTracker(..., recover_collection_instance=None)` with one pre-plan recovery path and recovered identity used by both Discogs fields.

- [ ] **Step 1: Write tracker recovery RED tests**

Extend tracker test helpers with an optional `AsyncMock` recovery callback. Add:

- `test_definitive_missing_recovers_same_release_new_instance_once`
- `test_abort_read_never_invokes_identity_recovery`
- `test_recovery_refusal_writes_neither_discogs_field`
- `test_second_definitive_missing_stops_both_field_writes`
- `test_replacement_abort_skips_play_count_but_updates_last_played_once`
- `test_recovered_play_count_failure_still_updates_last_played_on_replacement`
- `test_recovery_uses_latched_key_stale_pair_and_observed_tuple`
- `test_recovery_callback_is_spent_at_most_once`
- `test_missing_recovery_callback_preserves_fail_closed_behavior`

For success, the writer returns MISSING for `(999, 77)` with `(88,)`, then READY for `(999, 88)`. Assert the callback is awaited once with the exact four arguments, `set_play_count` targets 88 once (or repeats one absolute value under its existing retry), `update_last_played` targets 88 once, and the detached session is credited.

- [ ] **Step 2: Add #186/#229 recovery RED tests**

In `tests/test_credit_idempotent_186.py`, add:

- `test_ambiguous_old_instance_post_never_invokes_recovery`
- `test_recovered_absolute_post_retry_reuses_one_target`

The second test makes the first recovered POST apply server-side but lose its response. Every repeated body must equal the one target computed from the single replacement read; final server count is exactly old+1.

In `tests/test_finalize_retry_after.py`, add `test_rate_limited_replacement_read_does_not_replenish_recovery_budget`. Preserve all existing honored-wait bounds.

In `tests/test_listen_tracker_conc2.py`, prove a recovery finalizer does not hold `_lifecycle_lock`, and separate finalizers remain serialized.

- [ ] **Step 3: Write composition-root RED test**

In `tests/test_main_wiring.py`, capture constructed resolver/tracker and compare the injected bound method by `__self__` and `__func__`; do not use object identity between separate bound-method attribute reads.

Run:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_listen_tracker.py tests/test_credit_idempotent_186.py tests/test_finalize_retry_after.py tests/test_listen_tracker_conc2.py tests/test_main_wiring.py
```

Expected RED: tracker has no recovery port and main does not inject one.

- [ ] **Step 4: Implement one pre-plan recovery state machine**

Add the optional callback to `ListenTracker.__init__`. In `_credit_completed_album`, keep a local `recovery_spent = False`. When and only when the initial read is MISSING:

1. mark the budget spent before awaiting;
2. require callback and `session.album_resolve_key`;
3. forward the stale pair and immutable observed tuple;
4. stop both fields if recovery refuses;
5. update the detached session pair to the returned identity;
6. perform one replacement read; and
7. only READY may create the absolute count plan.

A second MISSING terminates both fields. ABORT on the replacement remains a failed Play Count attempt without another recovery; because the replacement identity was safely established, META-7 still makes one Last Played attempt against it. Never invoke recovery after `plan` contains an absolute target.

- [ ] **Step 5: Inject the narrow bound method**

In `main.py`, retain the resolver variable and construct:

```python
tracker = ListenTracker(
    DiscogsCollectionWriter(discogs_http, config.discogs),
    lastfm,
    recover_collection_instance=resolver.recover_collection_instance,
)
```

Do not pass the reader or resolver object itself and do not change the shared transport.

- [ ] **Step 6: Verify tracker/integration GREEN**

Run:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_listen_tracker.py tests/test_credit_idempotent_186.py tests/test_finalize_retry_after.py tests/test_listen_tracker_conc2.py tests/test_main_wiring.py tests/test_split_finalize_drain_187.py tests/test_listen_tracker_idempotency.py
git diff --check
```

- [ ] **Step 7: Independent recovery/retry review and commit**

Reviewer attacks: recovery after ambiguous POST, two recovery calls, a second missing result, reuse of old plan/IDs, Last Played on known-stale identity, META-7 suppression after safe replacement, callback absence/raise/cancel, and lifecycle-lock blockage. Resolve all findings and commit explicit files.

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
git add src/tracking/listen_tracker.py main.py tests/test_listen_tracker.py tests/test_credit_idempotent_186.py tests/test_finalize_retry_after.py tests/test_listen_tracker_conc2.py tests/test_main_wiring.py
git commit -m 'feat: retry one credit on safe Discogs replacement'
```

---

### Task 5: Add cross-layer acceptance evidence and durable documentation

**Files:**
- Create: `tests/test_metadata_recovery_wave2.py`
- Modify: `docs/decisions/remediation-guardrails.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-08-22-wave2-metadata-recovery-design.md` only if implementation review resolves a documented interface detail
- Modify: `docs/superpowers/plans/2026-08-22-wave2-metadata-recovery.md` checkboxes/status only after fresh evidence

- [ ] **Step 1: Add the cross-layer acceptance matrix**

Use real resolver/tracker orchestration with mocked HTTP/reader/writer seams. Add:

- `test_failed_refresh_then_cooldown_skip_returns_database_uncached`
- `test_stale_instance_recovers_to_safe_replacement_and_credits_once`
- `test_duplicate_replacement_instances_never_select_or_cache_a_write_target`

The first test records two displayed database results, zero cache entries, and no second page walk. The second begins with cached `(release=999, instance=77)`, gets a strict MISSING result carrying `(88,)`, rebuilds the same release, reads 88 once, and posts one absolute target. The duplicate test carries `(88, 89)`, clears only stale state, performs no field POST, and leaves the key uncached for the next play.

- [ ] **Step 2: Run the integrated Wave 2 suite**

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_cache_expiry.py tests/test_resolver.py tests/test_resolver_error_no_cache.py tests/test_discogs_collection_index.py tests/test_discogs_client.py tests/test_models.py tests/test_credit_idempotent_186.py tests/test_listen_tracker.py tests/test_listen_tracker_conc2.py tests/test_finalize_retry_after.py tests/test_main_wiring.py tests/test_metadata_recovery_wave2.py
git diff --check
```

- [ ] **Step 3: Update durable decisions and changelog**

In the Wave 2 guardrail section record:

- explicit refresh/cacheability states;
- strict complete-index promotion plus separate failed-build backoff;
- definitive missing proof only from validated single-page evidence;
- writer-carried immutable observed instance tuple;
- same release plus exactly one new instance for one recovery attempt;
- ordinary positive cache immortality and memory-only index unchanged;
- #186/#229/META-7 preserved.

In `CHANGELOG.md` under Unreleased, describe user-visible long-uptime ownership recovery and safe replacement crediting without claiming live Discogs validation.

- [ ] **Step 4: Independent documentation/spec review and commit**

Review current implementation, issue contracts, and historical #191/#242/#169/#186/#229/META-7 behavior. Reject any claim that ambiguous or multi-page absence is definitive or that positive entries now expire.

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
git add tests/test_metadata_recovery_wave2.py docs/decisions/remediation-guardrails.md CHANGELOG.md docs/superpowers/specs/2026-08-22-wave2-metadata-recovery-design.md docs/superpowers/plans/2026-08-22-wave2-metadata-recovery.md
git commit -m 'docs: record Wave 2 metadata recovery contract'
```

Omit unchanged paths from `git add`; never substitute a broad staging command.

---

### Task 6: Verify, adversarially review, and close Wave 2

**Files:**
- No production edits unless a confirmed review finding requires a new RED/GREEN fix round.
- Update ignored audit/log/memory artifacts only after tracked code is final.

- [ ] **Step 1: Run fresh local focused and full verification**

Run the integrated command from Task 5, then:

```bash
cd '/private/tmp/vnp-wave2-metadata-recovery'
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q
git diff --check origin/main...HEAD
git status --short --branch
```

Record the unsupported local Python version and any known environment-only failures separately from product failures.

- [ ] **Step 2: Run independent adversarial review**

Reviewers must attempt at least:

- failed refresh → cooldown skip → false database cache;
- incomplete/capped index promotion and repeated maximum page walks;
- 404/multi-page/malformed response promoted to definitive missing;
- duplicate-copy arbitrary selection or cache repopulation;
- stale cache deletion after a newer resolve;
- reader entry from finalizer without serialization;
- changed release or same stale instance accepted;
- recovery after an old absolute target/ambiguous POST;
- a second recovery budget or recomputed count target;
- Last Played targeting stale identity or losing META-7 independence; and
- persisted instance state or ordinary positive expiry.

Every confirmed finding receives a RED test, minimal fix, fresh focused/integrated runs, and another independent review. Stop only at zero open material findings.

- [ ] **Step 3: Push and require supported exact-SHA CI**

Push the reviewed branch, open the PR, and require successful `test (3.11)`, `test (3.12)`, `test (3.13)`, version metadata, and dependency audit for the exact head SHA. Do not merge from local Python 3.9 evidence alone.

- [ ] **Step 4: Merge and reconcile issues/milestone**

After owner-approved merge, verify post-merge main checks. Add exact SHA/run/test evidence to #420 and #421; close only when their acceptance criteria match the merged behavior. Update the Wave 2 milestone and the canonical ignored audit/log/shared memory with exact evidence and remaining risks. No live Discogs write is part of this closeout.
