"""Collection write access for the tracker (A-4).

`DiscogsCollectionWriter` owns the two writes the listen tracker performs —
incrementing the Play Count custom field and recording Last Played — plus the
collection-field metadata they need (the field-name → id map and the current
field value read).  It reaches the REST API through the shared
:class:`~src.metadata.discogs.transport.DiscogsHttp`.

It has no knowledge of the read side (search/tracklist/year) — that lives in
:class:`~src.metadata.discogs.reader.DiscogsReader`.
"""

import logging
from dataclasses import dataclass
from datetime import date
from enum import Enum, auto
from typing import Any, Optional, Union, TYPE_CHECKING
from urllib.parse import quote

from src.metadata.discogs.transport import DiscogsHttp, DiscogsRateLimited, _API_BASE, _as_id
from src.metadata.discogs.outcomes import PlayCountReadResult, PlayCountReadState
from src.util.clock import clock_is_trustworthy

if TYPE_CHECKING:
    from src.config import DiscogsConfig

log = logging.getLogger(__name__)


class _ReadFailed:
    """Sentinel type for :meth:`DiscogsCollectionWriter._get_field_value`.

    Means "the current field value could not be read" (an HTTP error, a network
    exception, or a 200 whose body does not contain the instance) and therefore
    must NOT be treated as ``0``. It is deliberately DISTINCT from ``None``,
    which means a *confirmed-blank* field and is safe to treat as ``0``.

    Conflating the two — every read failure collapsing to ``None`` and then to
    ``0`` — let a single failed read reset an accumulated Play Count to ``1``
    with a success log (finding META-1). A read-modify-write that ends in an
    absolute set must abort when the read cannot be trusted.
    """

    __slots__ = ()


#: Singleton "current value is unknown" marker (see :class:`_ReadFailed`).
_READ_FAILED = _ReadFailed()


class _FieldValueReadState(Enum):
    FOUND = auto()
    DEFINITIVE_INSTANCE_MISSING = auto()
    ABORT = auto()


@dataclass(frozen=True)
class _FieldValueRead:
    """Private classification before field-value parsing."""

    state: _FieldValueReadState
    value: Any = None
    observed_instance_ids: tuple[int, ...] = ()


class DiscogsCollectionWriter:
    """Increment Play Count and record Last Played on collection items."""

    def __init__(self, http: DiscogsHttp, config: "DiscogsConfig"):
        self._http = http
        self.username: str = config.username
        # SEC-7: percent-encode the username ONCE for use as a URL PATH SEGMENT.
        # It is operator-authored (config.yaml), so a value containing '/', '?'
        # or '#' would otherwise silently reshape the request path (extra
        # segments, a stray query string, a fragment). ``self.username`` stays
        # raw for identity/logging; every collection URL below uses this encoded
        # form. ``safe=""`` encodes ALL reserved characters, since the whole
        # value is a single segment (a normal alphanumeric username is unchanged).
        self._username_path: str = quote(config.username, safe="")
        self.play_count_field_name: str = config.play_count_field_name
        self.last_played_field_name: Optional[str] = config.last_played_field_name

        self._collection_fields: Optional[dict] = None  # Lazily fetched, then cached

    async def run(self, fn, *args):
        """Dispatch one of this writer's blocking methods on the shared,
        dedicated Discogs executor (#61) rather than the default pool.

        Thin delegate to :meth:`DiscogsHttp.run`; the transport owns the one pool
        both halves (reader + writer) share. The listen tracker calls
        ``await writer.run(writer.increment_play_count, release_id, instance_id)``
        in place of ``loop.run_in_executor(None, …)``, so the Play Count / Last
        Played writes — and any 429 backoff sleep — never touch the shared pool.
        """
        return await self._http.run(fn, *args)

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def read_play_count(self, release_id: int, instance_id: int) -> PlayCountReadResult:
        """Read the current Play Count and resolve the field id, in ONE step.

        Returns a typed state: ``READY`` with the resolved field/count,
        ``DEFINITIVE_INSTANCE_MISSING`` only when a complete validated snapshot
        proves the old instance is absent, or ``ABORT`` when writing is unsafe.

          * the ``Play Count`` custom field is not configured on the account;
          * the current value is UNKNOWN (``_READ_FAILED`` — the GET failed or the
            instance was absent/paged), so treating it as 0 would clobber the
            owner's accumulated count (META-1);
          * the value is present but non-integer real data (META-2).

        A blank field (``None``) is a CONFIRMED zero.  This is the READ half of
        the read-once/idempotent-set credit (#186): the caller reads ONCE, computes
        ``current_count + 1``, and retries only the absolute POST — so an ambiguous
        POST (applied server-side but response lost) can never be re-read and
        double-credited.  A raised :class:`DiscogsRateLimited` propagates for the
        #229 read-honor path.
        """
        fields = self._get_collection_fields()
        field_id = fields.get(self.play_count_field_name)
        if field_id is None:
            # R6-09: a custom field created on Discogs AFTER boot must be picked
            # up without a restart. Drop the cached (fieldless) map so the NEXT
            # credit re-fetches instead of re-aborting forever against the stale
            # cache — one extra GET per failed credit, paid only while broken.
            self._collection_fields = None
            log.error(
                f"Custom field '{self.play_count_field_name}' not found in Discogs. "
                f"Available fields: {list(fields.keys())}"
            )
            return PlayCountReadResult(PlayCountReadState.ABORT)

        field_read = self._read_field_value(release_id, instance_id, field_id)
        if field_read.state is _FieldValueReadState.DEFINITIVE_INSTANCE_MISSING:
            return PlayCountReadResult(
                PlayCountReadState.DEFINITIVE_INSTANCE_MISSING,
                observed_instance_ids=field_read.observed_instance_ids,
            )
        if field_read.state is _FieldValueReadState.ABORT:
            log.error(
                f"Aborting Play Count increment for release {release_id} / instance "
                f"{instance_id}: could not read the current value, so refusing to "
                f"overwrite it (leaving the existing count intact)."
            )
            return PlayCountReadResult(PlayCountReadState.ABORT)

        # Coerce via str() before .strip(): a confirmed value is normally a
        # string, but Discogs can return it as a JSON number, and calling
        # .strip() on an int would raise (B-16). None / "" is a blank field.
        raw_value = field_read.value
        text = str(raw_value).strip() if raw_value is not None else ""
        if not text:
            return PlayCountReadResult(PlayCountReadState.READY, field_id, 0)
        try:
            count = int(text)
            if count < 0:
                raise ValueError("negative play count")
            return PlayCountReadResult(PlayCountReadState.READY, field_id, count)
        except (ValueError, TypeError):
            # A present but non-integer value is real data we cannot safely
            # increment; overwriting it with an absolute 1 would destroy it
            # (META-2). Abort rather than clobber.
            log.error(
                f"Aborting Play Count increment for release {release_id} / instance "
                f"{instance_id}: existing field value is not an integer or is negative; "
                f"refusing to overwrite it with 1."
            )
            return PlayCountReadResult(PlayCountReadState.ABORT)

    def set_play_count(
        self, release_id: int, instance_id: int, field_id: int,
        current_count: int, new_count: int,
    ) -> bool:
        """POST an ABSOLUTE Play Count value.  Idempotent by construction: the
        body is ``new_count`` (not an increment), so re-issuing the SAME call
        writes the SAME value.  This is the retried unit of the #186 fix — the
        finalize layer computes ``new_count`` once (from :meth:`read_play_count`)
        and retries ONLY this, so an ambiguous POST that already landed is simply
        overwritten with the same value, never doubled.

        Returns True on HTTP 204, False on any other status.  Raises on transport
        failure / :class:`DiscogsRateLimited` so the caller's bounded retry (and
        the #229 honor path) can act; the caller treats a raise as one failed
        attempt.
        """
        # Validate every ID before it lands in the write URL (S-5).
        url = (
            f"{_API_BASE}/users/{self._username_path}/collection"
            f"/folders/0/releases/{_as_id(release_id, 'release_id')}"
            f"/instances/{_as_id(instance_id, 'instance_id')}"
            f"/fields/{_as_id(field_id, 'field_id')}"
        )
        # Idempotent absolute-set (writes new_count, not an increment), so a
        # single 429 retry is safe (B-15).  #229: honor_long_retry_after opts THIS
        # write in to the event-loop backoff — a long Retry-After raises
        # DiscogsRateLimited instead of losing the credit.  The absolute-set means
        # both the in-request 429 re-POST AND the finalize-layer retry (#186) write
        # the SAME new_count, so neither can double-credit.
        resp = self._http.request(
            "POST", url, retry_on_429=True, honor_long_retry_after=True,
            json={"value": str(new_count)},
        )
        if resp.status_code == 204:
            log.info(
                f"Play Count updated for release {release_id} / instance {instance_id}: "
                f"{current_count} → {new_count}."
            )
            return True
        # Log the status code only; the raw 4xx body is not logged (S-4).
        log.error(f"Discogs field update returned {resp.status_code}.")
        return False

    def increment_play_count(self, release_id: int, instance_id: int) -> bool:
        """Increment the 'Play Count' custom field by 1 for a collection item.

        A thin composition of :meth:`read_play_count` then :meth:`set_play_count`
        for callers that want a single read-modify-write in one call (and the
        existing unit tests).  Returns True on success (HTTP 204), False on any
        failure.

        NOTE: this is NOT the unit the finalize layer retries, and has NO
        production caller (R6-12) — the tracker credits via
        ``_credit_completed_album``, which calls :meth:`read_play_count` once and
        retries only :meth:`set_play_count`.  Retrying the whole read-modify-write
        double-credits an ambiguous-but-applied POST (#186), so do NOT route the
        tracker back through this method.  Retained as a tested single-call
        convenience (kept idempotent-on-429 within one call via the absolute-set
        POST) for operator tooling and the unit tests.
        """
        try:
            result = self.read_play_count(release_id, instance_id)
            if result.state is not PlayCountReadState.READY:
                return False
            return self.set_play_count(
                release_id, instance_id, result.field_id, result.current_count,
                result.current_count + 1,
            )
        except DiscogsRateLimited:
            # #229: must PROPAGATE, not be swallowed to False by the broad handler
            # below — the async finalize layer catches it to honor the wait and
            # retry. Swallowing it here would silently drop the credit (the very
            # loss #229 fixes).
            raise
        except Exception as e:
            log.error(f"Failed to increment Play Count for release {release_id}: {e}")
            return False

    def update_last_played(self, release_id: int, instance_id: int) -> bool:
        """Write today's date (ISO 8601, YYYY-MM-DD) to the 'Last Played' custom field.

        If last_played_field_name is not configured in config.yaml, this is a
        graceful no-op that returns True without making any API calls.

        Uses the Discogs collection field update endpoint:
          POST /users/{username}/collection/folders/0/releases/{release_id}
               /instances/{instance_id}/fields/{field_id}

        Returns True on success (HTTP 204) or if not configured, False on any failure.
        """
        if not self.last_played_field_name:
            return True  # Not configured — graceful no-op

        # Clock-sanity gate (STAB-2): the Pi has no RTC, so a pre-NTP boot makes
        # date.today() read the Unix epoch or a stale fake-hwclock date. Writing
        # that as an absolute set would stamp a wrong date over the correct Last
        # Played value in the real collection. Skip the write (with one WARNING)
        # rather than corrupt it; the next play re-attempts once the clock syncs.
        # Play Count is deliberately NOT gated — it writes a count, not a date.
        if not clock_is_trustworthy():
            log.warning(
                "Skipping Last Played update for release %s / instance %s: the system "
                "clock is not yet trustworthy (pre-NTP boot?) — refusing to overwrite the "
                "real value with a wrong date. It will update on the next play once the "
                "clock has synchronized.",
                release_id, instance_id,
            )
            return False

        try:
            fields = self._get_collection_fields()
            field_id = fields.get(self.last_played_field_name)
            if field_id is None:
                # R6-09: drop the cached map so a Last Played field created after
                # boot is picked up on the next write instead of aborting forever.
                self._collection_fields = None
                log.error(
                    f"Custom field '{self.last_played_field_name}' not found in Discogs. "
                    f"Available fields: {list(fields.keys())}"
                )
                return False

            today = date.today().isoformat()  # ISO 8601, e.g. "YYYY-MM-DD"

            # Validate every ID before it lands in the write URL (S-5).
            url = (
                f"{_API_BASE}/users/{self._username_path}/collection"
                f"/folders/0/releases/{_as_id(release_id, 'release_id')}"
                f"/instances/{_as_id(instance_id, 'instance_id')}"
                f"/fields/{_as_id(field_id, 'field_id')}"
            )
            # Idempotent absolute-set (writes today's date), so a single 429
            # retry is safe (B-15).
            resp = self._http.request("POST", url, retry_on_429=True, json={"value": today})

            if resp.status_code == 204:
                log.info(
                    f"Last Played updated for release {release_id} / instance {instance_id}: "
                    f"{today}."
                )
                return True

            # Log the status code only; the raw 4xx body is not logged (S-4).
            log.error(f"Discogs Last Played update returned {resp.status_code}.")
            return False

        except Exception as e:
            log.error(f"Failed to update Last Played for release {release_id}: {e}")
            return False

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def get_collection_fields(self) -> dict:
        """Public accessor for the collection's custom-field name→id map.

        Exposed as public API so operator tooling — ``scripts/discogs_live_check.py``
        and the first-boot smoke test — can read the field map through a supported
        seam instead of reaching into the private ``_get_collection_fields`` from
        outside the package, a cross-package private reach that a writer refactor
        would silently break (CRIT-6).  Internal callers keep using the private
        impl directly; this is a thin, stable facade over it.
        """
        return self._get_collection_fields()

    def _get_collection_fields(self) -> dict:
        """Lazily fetch and cache the user's collection custom field definitions.

        Returns a dict of {field_name: field_id}.
        """
        if self._collection_fields is not None:
            return self._collection_fields

        # #229: the fields map is the first GET in a cold-cache credit, so it
        # honors the long wait too — otherwise a first-credit-of-session landing
        # in a throttle window would 429 here and abort before the value read /
        # POST could honor it. Cached after one success, so this is paid at most
        # once per session. A long-429 raises DiscogsRateLimited (propagates to
        # the finalize layer); raise_for_status still handles other non-2xx.
        resp = self._http.request(
            "GET", f"{_API_BASE}/users/{self._username_path}/collection/fields",
            honor_long_retry_after=True,
        )
        resp.raise_for_status()
        data = resp.json()
        self._collection_fields = {
            f["name"]: f["id"] for f in data.get("fields", [])
        }
        log.debug(f"Collection fields loaded: {self._collection_fields}")
        return self._collection_fields

    def _read_field_value(
        self, release_id: int, instance_id: int, field_id: int
    ) -> _FieldValueRead:
        """Classify a field read without confusing absence with ambiguity.

        A found target can be read immediately: it does not need a full snapshot
        proof.  When the target is absent, however, replacement evidence is
        exposed only after validating every item in one complete response page.
        """
        try:
            # #229: this GET is the READ half of the credit's read-modify-write,
            # so it opts into honor_long_retry_after too. In a real throttle window
            # EVERY request 429s — if only the POST honored the wait, a throttled
            # READ would return _READ_FAILED and abort the credit to False BEFORE
            # the POST honor-branch could fire, losing the credit to three futile
            # in-window retries (the exact failure #229 targets). Honoring here
            # raises DiscogsRateLimited (re-raised below) so the finalize layer
            # waits out the window and re-reads; the GET is idempotent, so the
            # honored re-read is free of side effects.
            resp = self._http.request(
                "GET",
                f"{_API_BASE}/users/{self._username_path}/collection"
                f"/releases/{_as_id(release_id, 'release_id')}",
                honor_long_retry_after=True,
            )
            if resp.status_code != 200:
                log.debug(
                    f"_get_field_value: GET returned {resp.status_code} "
                    f"for release {release_id}; current value UNKNOWN (not writing)."
                )
                return _FieldValueRead(_FieldValueReadState.ABORT)
            data = resp.json()
            if not isinstance(data, dict):
                return _FieldValueRead(_FieldValueReadState.ABORT)
            instances = data.get("releases")
            if not isinstance(instances, list):
                return _FieldValueRead(_FieldValueReadState.ABORT)
            for inst in instances:
                if (isinstance(inst, dict)
                        and type(inst.get("instance_id")) is int
                        and inst["instance_id"] == instance_id):
                    notes = inst.get("notes", [])
                    if not isinstance(notes, list):
                        return _FieldValueRead(_FieldValueReadState.ABORT)
                    for note in notes:
                        if not isinstance(note, dict):
                            return _FieldValueRead(_FieldValueReadState.ABORT)
                        if note.get("field_id") == field_id:
                            return _FieldValueRead(_FieldValueReadState.FOUND, note.get("value"))
                    # Instance found but this field is unset — a CONFIRMED blank,
                    # safe to treat as 0.
                    return _FieldValueRead(_FieldValueReadState.FOUND)

            observed_ids = self._validated_missing_instance_ids(data, release_id)
            if observed_ids is not None:
                return _FieldValueRead(
                    _FieldValueReadState.DEFINITIVE_INSTANCE_MISSING,
                    observed_instance_ids=observed_ids,
                )
            log.debug(
                f"_get_field_value: instance {instance_id} not found in "
                f"release {release_id} response; current value UNKNOWN (not writing)."
            )
            return _FieldValueRead(_FieldValueReadState.ABORT)
        except DiscogsRateLimited:
            # #229: propagate the honored-wait signal (the broad handler below
            # would otherwise swallow it to _READ_FAILED, aborting the credit
            # before the finalize layer could wait out the throttle window).
            raise
        except Exception as e:
            log.debug(f"_get_field_value failed for release {release_id}: {e}")
            return _FieldValueRead(_FieldValueReadState.ABORT)

    @staticmethod
    def _validated_missing_instance_ids(data: dict, release_id: int) -> Optional[tuple[int, ...]]:
        """Return snapshot instance IDs only when absence is conclusively proven."""
        pagination = data.get("pagination")
        instances = data.get("releases")
        if not isinstance(pagination, dict) or not isinstance(instances, list):
            return None
        try:
            page = pagination["page"]
            pages = pagination["pages"]
            items = pagination["items"]
            per_page = pagination["per_page"]
        except KeyError:
            return None
        if (any(type(value) is not int for value in (page, pages, items, per_page))
                or page != 1 or pages != 1 or items != len(instances)
                or items < 0 or per_page < 1 or per_page < items):
            return None

        observed_ids = []
        for instance in instances:
            if not isinstance(instance, dict):
                return None
            instance_id = instance.get("instance_id")
            folder_id = instance.get("folder_id")
            basic_information = instance.get("basic_information")
            if (type(instance_id) is not int or instance_id <= 0
                    or type(folder_id) is not int or folder_id < 0
                    or not isinstance(basic_information, dict)
                    or type(basic_information.get("id")) is not int
                    or basic_information["id"] != release_id):
                return None
            observed_ids.append(instance_id)
        if len(set(observed_ids)) != len(observed_ids):
            return None
        return tuple(observed_ids)

    def _get_field_value(
        self, release_id: int, instance_id: int, field_id: int
    ) -> Union[str, None, _ReadFailed]:
        """Compatibility value-only facade for legacy callers and tests."""
        result = self._read_field_value(release_id, instance_id, field_id)
        if result.state is _FieldValueReadState.FOUND:
            return result.value
        return _READ_FAILED
