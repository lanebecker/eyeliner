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
from datetime import date
from typing import Optional, Union, TYPE_CHECKING

from src.metadata.discogs.transport import DiscogsHttp, _API_BASE, _as_id
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


class DiscogsCollectionWriter:
    """Increment Play Count and record Last Played on collection items."""

    def __init__(self, http: DiscogsHttp, config: "DiscogsConfig"):
        self._http = http
        self.username: str = config.username
        self.play_count_field_name: str = config.play_count_field_name
        self.last_played_field_name: Optional[str] = config.last_played_field_name

        self._collection_fields: Optional[dict] = None  # Lazily fetched, then cached

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def increment_play_count(self, release_id: int, instance_id: int) -> bool:
        """Increment the 'Play Count' custom field by 1 for a collection item.

        Reads the current value first (defaulting to 0 if blank, unreadable,
        or unreachable), then POSTs the incremented value back.

        Uses the Discogs collection field update endpoint:
          POST /users/{username}/collection/folders/0/releases/{release_id}
               /instances/{instance_id}/fields/{field_id}

        Returns True on success (HTTP 204), False on any failure.
        """
        try:
            fields = self._get_collection_fields()
            field_id = fields.get(self.play_count_field_name)
            if field_id is None:
                log.error(
                    f"Custom field '{self.play_count_field_name}' not found in Discogs. "
                    f"Available fields: {list(fields.keys())}"
                )
                return False

            # Read the current value. This is a read-modify-write ending in an
            # ABSOLUTE set, so an untrusted read must never become a write:
            # treating an unreadable value as 0 would reset the owner's
            # accumulated Play Count to 1 (META-1). Only a CONFIRMED-blank field
            # (None) is a safe 0; _READ_FAILED means "unknown — abort".
            raw_value = self._get_field_value(release_id, instance_id, field_id)
            if raw_value is _READ_FAILED:
                log.error(
                    f"Aborting Play Count increment for release {release_id} / instance "
                    f"{instance_id}: could not read the current value, so refusing to "
                    f"overwrite it (leaving the existing count intact)."
                )
                return False

            # Coerce via str() before .strip(): a confirmed value is normally a
            # string, but Discogs can return it as a JSON number, and calling
            # .strip() on an int would raise (B-16). None / "" is a blank field.
            text = str(raw_value).strip() if raw_value is not None else ""
            if not text:
                current_count = 0  # confirmed-blank field == zero plays
            else:
                try:
                    current_count = int(text)
                except (ValueError, TypeError):
                    # A present but non-integer value is real data we cannot
                    # safely increment; overwriting it with an absolute 1 would
                    # destroy it (META-2). Abort rather than clobber.
                    log.error(
                        f"Aborting Play Count increment for release {release_id} / instance "
                        f"{instance_id}: existing value {raw_value!r} is not an integer, so "
                        f"refusing to overwrite it with 1."
                    )
                    return False

            new_count = current_count + 1

            # Validate every ID before it lands in the write URL (S-5).
            url = (
                f"{_API_BASE}/users/{self.username}/collection"
                f"/folders/0/releases/{_as_id(release_id, 'release_id')}"
                f"/instances/{_as_id(instance_id, 'instance_id')}"
                f"/fields/{_as_id(field_id, 'field_id')}"
            )
            # Idempotent absolute-set (writes new_count, not an increment), so a
            # single 429 retry is safe (B-15).
            resp = self._http.request(
                "POST", url, retry_on_429=True, json={"value": str(new_count)}
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
                log.error(
                    f"Custom field '{self.last_played_field_name}' not found in Discogs. "
                    f"Available fields: {list(fields.keys())}"
                )
                return False

            today = date.today().isoformat()  # e.g. "2026-05-24"

            # Validate every ID before it lands in the write URL (S-5).
            url = (
                f"{_API_BASE}/users/{self.username}/collection"
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

    def _get_collection_fields(self) -> dict:
        """Lazily fetch and cache the user's collection custom field definitions.

        Returns a dict of {field_name: field_id}.
        """
        if self._collection_fields is not None:
            return self._collection_fields

        resp = self._http.request(
            "GET", f"{_API_BASE}/users/{self.username}/collection/fields"
        )
        resp.raise_for_status()
        data = resp.json()
        self._collection_fields = {
            f["name"]: f["id"] for f in data.get("fields", [])
        }
        log.debug(f"Collection fields loaded: {self._collection_fields}")
        return self._collection_fields

    def _get_field_value(
        self, release_id: int, instance_id: int, field_id: int
    ) -> Union[str, None, _ReadFailed]:
        """Read the current value of a custom field for a specific collection instance.

        GETs /users/{username}/collection/releases/{release_id}, finds the
        matching instance_id, and returns the note value for field_id.

        Three-state result, because the caller performs an absolute write and a
        failed read must NOT be treated as 0 (META-1):

          * ``str`` (possibly a JSON number the caller coerces) — the value is set.
          * ``None`` — the instance was found but this field is unset: a
            CONFIRMED-blank field, safe to treat as 0.
          * :data:`_READ_FAILED` — the value is UNKNOWN and the caller must
            abort: the GET failed, an exception was raised, or the instance was
            not present in the 200 body (absent / paged / edited — ambiguous).
        """
        try:
            resp = self._http.request(
                "GET",
                f"{_API_BASE}/users/{self.username}/collection"
                f"/releases/{_as_id(release_id, 'release_id')}",
            )
            if resp.status_code != 200:
                log.debug(
                    f"_get_field_value: GET returned {resp.status_code} "
                    f"for release {release_id}; current value UNKNOWN (not writing)."
                )
                return _READ_FAILED
            instances = resp.json().get("releases", [])
            for inst in instances:
                if inst.get("instance_id") == instance_id:
                    for note in inst.get("notes", []):
                        if note.get("field_id") == field_id:
                            return note.get("value")
                    # Instance found but this field is unset — a CONFIRMED blank,
                    # safe to treat as 0.
                    return None
            # The instance is not in the response at all: ambiguous (genuinely
            # absent, a paged response, or an edited collection), so the current
            # value is UNKNOWN — not blank.
            log.debug(
                f"_get_field_value: instance {instance_id} not found in "
                f"release {release_id} response; current value UNKNOWN (not writing)."
            )
            return _READ_FAILED
        except Exception as e:
            log.debug(f"_get_field_value failed for release {release_id}: {e}")
            return _READ_FAILED
