"""Recognition loop — polls for track identity while music plays.

Abstracts the recognition backend behind RecognizerBackend so a backend
(ShazamIO and AudD implemented; ACRCloud planned) can be swapped via config
without touching this file. config.py's CRIT-2 gate rejects an unimplemented
backend (e.g. ACRCloud) at startup until it is built.
"""

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

import numpy as np

from src.audio.log_throttle import ThrottledLogger
from src.config import IMPLEMENTED_BACKENDS
from src.state.player_state import PlayerStatus
from src.util.logthrottle import LogThrottle

if TYPE_CHECKING:
    from src.config import RecognitionConfig
    from src.state.player_state import PlayerState

log = logging.getLogger(__name__)

# How many consecutive unconfirmable results to tolerate before logging a churn
# warning (B-21).  At ~10-12s/chunk this is ~a minute of "seeing tracks but
# never the same one twice" — enough to distinguish genuine churn from a normal
# track change, cheap enough to leave a journal breadcrumb when it happens.
_CHURN_LOG_EVERY = 5

# PCONC-2: hard bound on a single recognition call (~3× the ~10s chunk hop
# cadence — chunk_seconds minus overlap_seconds at the defaults). A DEDICATED
# constant, deliberately NOT poll_interval: reusing the idle-poll timeout would
# mean tuning the poll rate DOWN silently caps every real Shazam round-trip
# (routinely 3–8s) below its runtime, timing out every recognition and latching
# the display to ERROR. 30s abandons a degraded call before the maxsize-5 queue
# saturates (PCONC-1) while leaving ample headroom over a normal call.
_RECOGNIZE_TIMEOUT_SECONDS = 30

# PCONC-2: pin shazamio's HTTP retry policy instead of inheriting its default
# (attempts=20 × max_timeout=60), which lets ONE degraded recognize retry for
# minutes and occupy the recognition loop. `attempts` is the operative bound: a
# couple of tries fails fast (the loop already re-tries on the next chunk and
# confirmation needs several matches). `max_timeout` is aiohttp_retry's BACKOFF
# cap, not a per-request timeout — with attempts=2 the single backoff is ~0.2s so
# it never binds, but it is pinned low anyway so we never inherit the 60s default.
# run()'s wait_for is the hard backstop that actually bounds a hung request.
_SHAZAM_RETRY_ATTEMPTS = 2
_SHAZAM_RETRY_MAX_TIMEOUT_SECONDS = 5

# #197: minimum wall-clock gap between repeated recognition-FAILURE logs, on the
# #178 model already used in capture.py. Recognition is attempted on every chunk
# hop (~10s at the defaults), 24/7. A sustained network outage makes EVERY hop
# fail, so an unthrottled failure line would write ~8,640 identical records/day —
# the flood class #178/PCONC-4 already throttle on the capture leg. Both
# recognition failure legs are throttled through ThrottledLogger: the fast-fail
# except in ShazamIOBackend.recognize (connection refused / DNS — fails inside
# the call) AND run()'s loop-error handler (a HUNG outage that black-holes
# packets is cancelled by the recognize_timeout wait_for BEFORE recognize's
# except can log, and lands there instead). Throttling only one leg would just
# move the flood between WARNING and ERROR depending on the outage's shape.
# 60s (vs capture's 30s) keeps at most ~1,440 lines/day worst case.
_RECOGNITION_ERROR_LOG_INTERVAL_SECONDS = 60.0

# R10-11 (#424): minimum wall-clock gap between "recognition queue lag" health
# lines. When the backend stalls, the consumer drains stale backlog to the newest
# chunk (see run()) and reports the lag; throttled on the #178 model so a
# sustained slow-backend period logs one line then a periodic summary, not one
# per hop.
_QUEUE_LAG_LOG_INTERVAL_SECONDS = 60.0

# #454: per-track recognition scheduling. Recognize a few seconds PAST a predicted
# boundary so we sample INTO the new track (past the old track's tail).
_BOUNDARY_MARGIN_SECONDS = 3.0
# Floor on the idle wait, so a match near a track's end (or slightly-off duration
# data) still waits a beat rather than re-recognizing on the very next hop.
_MIN_REACTIVATE_SECONDS = 10.0
# When a reactivation finds the SAME track still playing (prediction ran early, or
# the turntable is a touch slow), re-idle this long instead of polling every hop.
_SAME_TRACK_RECHECK_SECONDS = 20.0
# #460: while UNIDENTIFIED (NO MATCH FOUND latched), retry this often — short enough
# that two consecutive wakes land inside the same later track, so it reaches the
# 2-match confirmation and a failed opener no longer strands the rest of a gapless
# side. Distinct from the (longer) between-known-tracks safety interval; still far
# above the ~10s hop, so an all-instrumental side stays bounded (~2 requests/min).
_ERROR_RETRY_SECONDS = 30.0

# #464: while a candidate is mid-confirmation we poll at the fast chunk rate to catch
# its confirming second hit before the track ends. Cap the burst: after this many
# non-confirming attempts (misses OR unconfirmable churn) void the candidate and
# resume the slow back-off, so a stray/one-off misrecognition can't fast-poll the
# backend forever. The FIRST hit consumes one attempt (it goes through the churn
# branch), so a cap of 7 leaves ~6 chunk hops (~60s at a 10s hop) for the confirming
# second hit — comfortably longer than the gap between two real hits of a playing
# track — while bounding a stray's burst to ~7 fast polls before the 30s back-off
# resumes. (A sustained fresh-misrecognition-every-window side is still bounded, at
# ~2.25x the idle rate — acceptably rare and far under the AudD quota.)
_CANDIDATE_CONFIRM_ATTEMPTS = 7

# #472: opt-in per-poll recognition diagnostics — each backend result (with its
# match offset + the confirmation state) and each predicted next-track boundary.
# For debugging track skips / mis-timing on real hardware WITHOUT hand-patching the
# source. Off by default: one bool check per poll, zero output. Enable by setting
# EYELINER_DEBUG_RECOGNITION to a truthy value (1/true/yes/on) in the environment
# (e.g. the systemd unit's Environment=). Emitted at INFO so it shows under the
# default log level. See docs/recognition-backends.md ("Debugging recognition").
_DEBUG_RECOGNITION = os.environ.get(
    "EYELINER_DEBUG_RECOGNITION", ""
).strip().lower() in ("1", "true", "yes", "on")


def _parse_mmss(value) -> Optional[float]:
    """Parse a Discogs duration / AudD timecode ("M:SS" or "H:MM:SS") to seconds.

    Returns None for empty, non-string, or malformed input so callers fall back to
    gap-detection / the safety timer rather than trusting a bogus boundary time."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if not (2 <= len(parts) <= 3) or not all(p.isascii() and p.isdigit() for p in parts):
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return float(secs)


@dataclass
class RawRecognitionResult:
    """Minimal result from any recognition backend."""
    title: str
    artist: str
    album: str
    isrc: Optional[str] = None
    confidence: Optional[float] = None
    # Seconds into the track where recognition matched (AudD's ``timecode``); None
    # when the backend doesn't report one (ShazamIO). #454 uses it with the Discogs
    # track duration to predict the next track boundary.
    match_offset: Optional[float] = None


class RecognizerBackend(ABC):
    """Interface all recognition backends must implement."""

    @abstractmethod
    async def recognize(
        self, audio: np.ndarray, sample_rate: int
    ) -> Optional[RawRecognitionResult]:
        """Identify a chunk of audio. Returns None if unrecognized."""
        ...


class ShazamIOBackend(RecognizerBackend):
    """Recognition via ShazamIO (unofficial Shazam API — free, personal use).

    The Shazam client is created once on first use and reused for every
    subsequent recognition (v1.3.3) — constructing a fresh client per chunk
    threw away its internal HTTP session several times a minute for no
    benefit.  recognize() is split into three isolated stages (A-13):
    `_encode_wav` (executor), `_call_shazam` (transport), and the pure
    `_parse_shazam` (response-shape parsing).  The shazamio import (_call_shazam)
    and soundfile import (_encode_wav) are kept lazy on purpose: they keep this
    module importable (and the rest of the suite testable) on machines without
    the audio stack installed.
    """

    def __init__(self):
        self._shazam = None  # Created lazily on first recognize()
        # #197: throttle the per-chunk failure WARNING. A network outage fails
        # this call every ~10s hop, 24/7; unthrottled that is ~8,640 identical
        # journal lines/day (the #178 flood class). First failure and any changed
        # message log at once; identical repeats summarise at most once per
        # _RECOGNITION_ERROR_LOG_INTERVAL_SECONDS; a success flushes the tally.
        self._error_log = ThrottledLogger(
            log, _RECOGNITION_ERROR_LOG_INTERVAL_SECONDS, level=logging.WARNING
        )

    async def recognize(
        self, audio: np.ndarray, sample_rate: int
    ) -> Optional[RawRecognitionResult]:
        # Three isolated stages (A-13): encode (executor) → call Shazam
        # (transport) → parse (pure).  One broad except is the true boundary
        # back to the recognition loop, which treats any failure as a miss.
        try:
            # Serialize the chunk to an in-memory WAV in an executor — soundfile's
            # sf.write is a blocking C call (~1.3 MB encode from a ~2.6 MB float32
            # chunk) that would otherwise stall the event loop inline (P-6).
            loop = asyncio.get_running_loop()
            wav_bytes = await loop.run_in_executor(
                None, self._encode_wav, audio, sample_rate
            )
            result = await self._call_shazam(wav_bytes)
            parsed = self._parse_shazam(result)
            # FULL success (transport AND parse) — the failure (if any) is over,
            # so flush the throttle: the streak count is reported and the next
            # failure logs immediately (#197). Deliberately AFTER _parse_shazam,
            # not after _call_shazam: a truthy-but-malformed response (e.g. a
            # top-level JSON list) makes _parse_shazam raise on EVERY chunk, and
            # resetting on transport-only success would clear the streak each hop
            # so that identical parse error logged every ~10s forever — re-opening
            # the very flood this throttle closes. Skipping reset on a parse raise
            # keeps the repeat throttled. A healthy miss (parse returns None) is a
            # real success and still resets here.
            self._error_log.reset()
            return parsed
        except Exception as e:
            # #197: route through the throttle instead of logging every chunk.
            self._error_log.error(f"ShazamIO recognition failed: {e}")
            return None

    async def _call_shazam(self, wav_bytes: bytes) -> dict:
        """Transport-only: lazily build the Shazam client and call it.

        The shazamio import is kept lazy here so the module stays importable
        (and the suite testable) on machines without the audio stack (A-13).
        """
        from shazamio import Shazam, HTTPClient
        from aiohttp_retry import ExponentialRetry

        if self._shazam is None:
            # PCONC-2: pin the retry policy rather than inheriting shazamio's
            # default (attempts=20, max_timeout=60), which lets one degraded call
            # retry for minutes and occupy the recognition loop.
            self._shazam = Shazam(
                http_client=HTTPClient(
                    retry_options=ExponentialRetry(
                        attempts=_SHAZAM_RETRY_ATTEMPTS,
                        max_timeout=_SHAZAM_RETRY_MAX_TIMEOUT_SECONDS,
                        statuses={500, 502, 503, 504, 429},
                    )
                )
            )
        return await self._shazam.recognize(wav_bytes)

    @staticmethod
    def _parse_shazam(result: dict) -> Optional[RawRecognitionResult]:
        """Pure parse of a Shazam JSON response → RawRecognitionResult or None.

        No I/O, no imports — unit-testable against captured JSON, isolating the
        fragile Shazam response-shape knowledge from transport (A-13).
        """
        track = (result or {}).get("track")
        if not track:
            return None
        # #167: a truthy but non-dict `track` (e.g. a JSON list) would make the
        # `track.get(...)` reads below raise AttributeError, escaping this pure
        # parser to recognize()'s broad except — a miss logged as a spurious
        # "recognition failed". Treat any non-dict track as a clean no-match. (The
        # null-CONTAINER shapes #167 also named — null sections/metadata/list
        # entries — are already handled below by REC-5's `or []` + album try/except.)
        if not isinstance(track, dict):
            return None

        # The title is the track's IDENTITY: a track object with an empty,
        # missing, or null title is a no-match, not a recognition (REC-3).
        # Without this guard, two such junk responses match each other and
        # "confirm" as a real track that then gets committed — and a null title
        # would later crash the dedup comparison in _same_track (the null-title
        # half of REC-2).  `or ""` coerces a JSON-null title to a string so the
        # emptiness check is None-safe.  Returning None makes the loop count it
        # as a miss instead of a candidate.
        # R5-10: str()-coerce — an untrusted payload can carry a NUMBER title/
        # subtitle/album, which passes the `or ""` truthiness as a non-string and
        # later crashes `.strip()`/`_norm().split()` on EVERY chunk (no miss
        # counted, display stuck on IDENTIFYING). `str(x or "")` maps null/0/""
        # to "" and any other value to its text form.
        title = str(track.get("title") or "")
        if not title.strip():
            return None

        # Pull album from the metadata section if present.  Break BOTH loops as
        # soon as the album is found — without the outer break the inner break
        # only exits the metadata loop and a later section could overwrite it.
        # REC-2: every string read is `… or ""` so a JSON null (key present, value
        # null) coerces to "" instead of None — a null metadata `title` would
        # otherwise crash `.lower()` here, and a null `text` would put None into
        # `album`.
        # REC-5: album is OPTIONAL, so its parse must never sink an otherwise-valid
        # title/artist match. `… or []` handles a JSON-null `sections`/`metadata`
        # (key present, value null → `.get(k, [])` returns None, not the default,
        # so `for … in None` would raise TypeError and recognize()'s broad except
        # would discard the whole response as a miss). The try/except is a second
        # line of defence against any other malformed album shape: on failure we
        # log and leave `album == ""` rather than losing the match.
        album = ""
        try:
            for section in track.get("sections") or []:
                for meta in section.get("metadata") or []:
                    if (meta.get("title") or "").lower() == "album":
                        album = str(meta.get("text") or "")
                        break
                if album:
                    break
        except Exception as e:
            log.warning(f"Shazam album parse failed; keeping title/artist match: {e!r}")
            album = ""

        # REC-2: coerce a JSON-null subtitle to "" (was None) — otherwise
        # _same_track's `artist.strip()` raises AttributeError inside
        # _handle_result, which escapes to run()'s handler so NO miss is counted
        # and the display sits on IDENTIFYING forever.  (title is already coerced +
        # emptiness-guarded above, REC-3.)
        isrc = track.get("isrc")
        return RawRecognitionResult(
            title=title,
            artist=str(track.get("subtitle") or ""),   # R5-10: numeric subtitle -> str
            album=album,
            isrc=str(isrc) if isrc is not None else None,  # R5-10: numeric isrc -> str
        )

    @staticmethod
    def _encode_wav(audio: np.ndarray, sample_rate: int) -> bytes:
        """Serialize an audio chunk to in-memory WAV bytes (PCM_16).

        Pure CPU/IO with no event-loop interaction, so it runs in an executor
        (see recognize) — sf.write is a blocking C call (P-6).
        """
        import io
        import soundfile as sf

        buf = io.BytesIO()
        sf.write(buf, audio, sample_rate, format="WAV", subtype="PCM_16")
        return buf.getvalue()


# ---------------------------------------------------------------------------
# AudD backend (#453) — a commercial, maintained recognition API and the
# recommended user-selectable alternative to ShazamIO (which stays the free,
# zero-config default). Needs a token in recognition.audd.api_token.
# ---------------------------------------------------------------------------

_AUDD_API_URL = "https://api.audd.io/"
# Internal per-request bound. run()'s wait_for(recognize_timeout, default 30s) is
# the hard backstop; this keeps one stuck HTTP call from riding that full budget
# (mirrors ShazamIO's pinned retry bound).
_AUDD_HTTP_TIMEOUT_SECONDS = 20


class _AuddApiError(Exception):
    """AudD returned status=error (bad token, exhausted quota, etc.). Raised
    inside the pure parser so recognize()'s one broad except routes it through the
    throttled failure log and counts it as a miss — a bad key or quota can never
    crash the recognition loop."""


class AuddBackend(RecognizerBackend):
    """Recognition via the AudD API (https://audd.io).

    recognize() reuses ShazamIOBackend._encode_wav for the identical in-memory
    WAV serialization, then POSTs the clip to AudD. The aiohttp import in
    _call_audd is lazy on purpose (as with shazamio) so this module stays
    importable — and _parse_audd unit-testable — without the HTTP stack.
    """

    def __init__(self, api_token: str):
        self._api_token = api_token or ""
        # #197 model: throttle the per-chunk failure WARNING (a bad token or a
        # network outage fails every ~10s hop otherwise — the #178 flood class).
        self._error_log = ThrottledLogger(
            log, _RECOGNITION_ERROR_LOG_INTERVAL_SECONDS, level=logging.WARNING
        )

    async def recognize(
        self, audio: np.ndarray, sample_rate: int
    ) -> Optional[RawRecognitionResult]:
        try:
            loop = asyncio.get_running_loop()
            wav_bytes = await loop.run_in_executor(
                None, ShazamIOBackend._encode_wav, audio, sample_rate
            )
            result = await self._call_audd(wav_bytes)
            parsed = self._parse_audd(result)
            self._error_log.reset()
            return parsed
        except Exception as e:
            self._error_log.error(f"AudD recognition failed: {e}")
            return None

    async def _call_audd(self, wav_bytes: bytes) -> dict:
        """Transport-only: POST the WAV to AudD. aiohttp import kept lazy (A-13)."""
        import aiohttp

        data = aiohttp.FormData()
        data.add_field("api_token", self._api_token)
        data.add_field(
            "file", wav_bytes, filename="audio.wav", content_type="audio/wav"
        )
        timeout = aiohttp.ClientTimeout(total=_AUDD_HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_AUDD_API_URL, data=data) as resp:
                # content_type=None: AudD replies application/json; stay lenient if
                # a proxy mislabels it rather than raising on the content type.
                return await resp.json(content_type=None)

    @staticmethod
    def _parse_audd(result) -> Optional[RawRecognitionResult]:
        """Pure parse of an AudD JSON response -> RawRecognitionResult or None.

        success + track -> result; success + null (no match) -> None;
        status=error -> raise _AuddApiError so recognize() logs it (throttled) and
        counts a miss. Every field is str()-coerced and null-guarded, mirroring
        _parse_shazam (untrusted external JSON; REC-2/REC-3/R5-10)."""
        if not isinstance(result, dict):
            return None
        status = result.get("status")
        if status == "error":
            err = result.get("error")
            msg = err.get("error_message") if isinstance(err, dict) else None
            raise _AuddApiError(str(msg or "AudD API returned an error"))
        if status != "success":
            return None
        res = result.get("result")
        # A null result is a clean no-match; a non-dict result is malformed -> miss.
        if not isinstance(res, dict):
            return None
        title = str(res.get("title") or "")
        if not title.strip():
            return None
        return RawRecognitionResult(
            title=title,
            artist=str(res.get("artist") or ""),
            album=str(res.get("album") or ""),
            isrc=None,
            match_offset=_parse_mmss(res.get("timecode")),
        )


class RecognitionLoop:
    """Manages the async recognition polling loop.

    Requires `confirmation_required` consecutive identical results before
    committing a track change, avoiding flickering on noisy matches.
    """

    def __init__(
        self,
        config: "RecognitionConfig",
        state: "PlayerState",
        on_confirmed: Callable[["RawRecognitionResult", int], Awaitable[object]],
        backend=None,
        hop_seconds: Optional[float] = None,
    ):
        self.state = state
        # Called with a confirmed RawRecognitionResult; owns resolve → state →
        # track → scrobble (A-9).  The loop awaits it but doesn't inspect the
        # result — it no longer knows about the resolver/tracker/lastfm.
        self.on_confirmed = on_confirmed
        self.poll_interval: int = config.poll_interval_seconds
        # R10-11 (#424): a queued chunk is "stale" once it has waited longer than
        # ONE CAPTURE HOP (chunk_seconds − overlap_seconds) — the cadence at which
        # fresh audio arrives — because past that the consumer has fallen behind.
        # The hop lives in the AUDIO config, not recognition, so main() passes it
        # in; a direct construction (tests) that omits it falls back to
        # poll_interval, which still bounds the drain/telemetry.  This is the
        # threshold both the drain-to-newest and the queue-age telemetry use, so
        # lag is surfaced "when it exceeds one hop" (#424 acceptance) rather than
        # only at the much coarser idle-poll interval.
        # Defensive: a None or non-positive hop (a degenerate/misconfigured value)
        # falls back to poll_interval so the threshold can never be ≤ 0 — which
        # would make EVERY dequeued chunk "stale" and drain fresh audio every turn.
        self._stale_after_seconds: float = (
            hop_seconds if (hop_seconds is not None and hop_seconds > 0)
            else self.poll_interval
        )
        # PCONC-2: bound a single recognize() call, decoupled from poll_interval.
        self.recognize_timeout: float = _RECOGNIZE_TIMEOUT_SECONDS
        self.confirmation_required: int = config.confirmation_required
        # Consecutive failed recognitions while LISTENING before the display
        # shows the error state (v1.4.1).  At ~10-12s per chunk, the default
        # of 6 puts "NO MATCH FOUND" on screen after roughly a minute of
        # music that ShazamIO can't identify.
        self.error_after_misses: int = config.error_after_misses
        self.backend_name: str = config.backend
        # AudD backend credential (validated required-when-audd in config.py).
        self._audd_api_token: str = config.audd_api_token
        # #454: per-track recognition gating. Recognition is "active" until a track
        # confirms, then idles until the predicted next-track boundary (or a needle
        # drop). Gates the COSTLY backend call only — capture/silence run unchanged.
        self._recognition_active: bool = True
        self._reactivate_at: float = 0.0
        self._safety_recheck_seconds: float = config.max_idle_recheck_seconds
        self._last_seen_session_epoch: int = 0
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        # R10-11 (#424): throttled health signal for recognition-queue lag. Single
        # key (the message carries the varying age/drop numbers, formatted at emit)
        # so a sustained slow-backend stretch collapses to one line + a periodic
        # summary rather than minting a throttle key per distinct age.
        self._queue_lag_throttle = LogThrottle(interval=_QUEUE_LAG_LOG_INTERVAL_SECONDS)
        self._pending_result: Optional[RawRecognitionResult] = None
        self._pending_count: int = 0
        # REC-1 review: the session epoch the pending candidate was built under.
        # A pending is void once a new session begins (a needle lift bumps
        # session_epoch via clear()), so a leftover count can't let a single
        # spurious hit confirm a stale track into the NEXT record's session.
        self._pending_epoch: int = 0
        # #464: non-confirming recognition attempts spent on the current candidate
        # burst; bounds the fast-poll so a stray pending can't hammer the backend.
        self._confirm_attempts: int = 0
        # PCONC-3: the epoch of the last chunk _handle_result saw, so a session
        # boundary (needle lift → session_epoch bump) can reset the per-session
        # health counters below. Epochs only increase and chunks are handled
        # oldest-first, so this changes once per real boundary, not on churn.
        self._last_epoch: int = 0
        self._miss_count: int = 0
        # Consecutive unconfirmable non-None results (alternating matches that
        # never reach confirmation_required).  Purely diagnostic — it leaves a
        # breadcrumb when the display "stops updating" because recognition is
        # churning rather than failing outright (B-21).
        self._churn_count: int = 0
        # ARCH-8: optional injection seam — defaults to selecting the configured
        # backend (with CRIT-2 validation), but a test can pass a substitute
        # instead of patching _init_backend. When injected, backend_name-based
        # selection/validation is intentionally bypassed (the caller owns the
        # choice).
        self.backend: RecognizerBackend = (
            backend if backend is not None else self._init_backend()
        )
        # #197: throttle run()'s loop-error ERROR. A network outage that HANGS
        # (black-holed packets, not connection-refused) is cancelled by the
        # recognize_timeout wait_for BEFORE ShazamIOBackend's own except can log,
        # so it floods HERE — ~1 line per (timeout + 2s sleep) ≈ every 32s, ~2,700
        # lines/day — the same class the backend throttle catches on the fast-fail
        # leg. Both must be throttled or the flood just moves with the outage's
        # shape. A clean recognize+commit flushes the streak (below).
        self._loop_error_log = ThrottledLogger(
            log, _RECOGNITION_ERROR_LOG_INTERVAL_SECONDS, level=logging.ERROR
        )

    def _init_backend(self) -> RecognizerBackend:
        # CRIT-2: config validation (RecognitionConfig, against the shared
        # IMPLEMENTED_BACKENDS set) rejects an unimplemented backend BEFORE we get
        # here, so in the normal flow this raise is unreachable — it is a
        # defensive backstop for direct construction (tests, or a future caller
        # that bypasses config). Validating against the same set keeps config and
        # construction from ever drifting.
        if self.backend_name not in IMPLEMENTED_BACKENDS:
            raise ValueError(
                f"Unknown recognition backend: {self.backend_name!r} "
                f"(implemented: {sorted(IMPLEMENTED_BACKENDS)})"
            )
        if self.backend_name == "shazamio":
            return ShazamIOBackend()
        if self.backend_name == "audd":
            return AuddBackend(self._audd_api_token)
        # A name that is in IMPLEMENTED_BACKENDS but has no constructor branch
        # above is a programming error — someone widened the set but not this
        # method. (TODO: add AcrcloudBackend when implemented.)
        raise ValueError(  # pragma: no cover
            f"recognition backend {self.backend_name!r} is allowed but has no "
            f"constructor in _init_backend."
        )

    async def enqueue(self, audio: np.ndarray, sample_rate: int):
        """Called by AudioCapture to hand off a chunk for recognition.

        If the recognition queue is full (Shazam is taking longer than capture
        is producing), drop the OLDEST queued chunk and admit this one
        (v1.3.5) — the freshest audio is the most relevant for detecting a
        track change, and this matches AudioCapture's block-queue policy.
        (Previously the incoming chunk was discarded, so a lagging backend
        kept grinding through stale audio and delayed track-change
        detection.)  Drops are logged at debug level so a "stopped
        identifying" complaint has a breadcrumb in the journal.

        The current session epoch is captured HERE, at enqueue (≈ capture) time,
        and travels with the chunk (PCONC-1).  A chunk can sit in this queue
        while the needle lifts and a new session begins; binding the epoch at
        enqueue — not when the chunk is later dequeued, recognized, or committed
        — is what lets the commit service recognise audio that predates the live
        session and discard it instead of committing a dead track into a fresh
        one.  (Dequeue-time would already read the post-lift epoch and defeat the
        guard.)
        """
        # Bind the session epoch to the audio at capture time (PCONC-1).
        epoch = self.state.session_epoch
        # R10-11 (#424): stamp the enqueue (≈ capture) time so run() can measure how
        # stale a chunk is when it is finally dequeued and surface queue lag.
        # Monotonic (not wall clock) so an NTP step during a pre-sync boot can't
        # produce a negative or wildly wrong age.
        enqueued_at = time.monotonic()
        if self._audio_queue.full():
            try:
                self._audio_queue.get_nowait()  # Drop the OLDEST — recent audio wins
                log.debug(
                    "Recognition queue full (maxsize=%d); dropped the oldest chunk. "
                    "If this happens consistently, recognition is slower than capture.",
                    self._audio_queue.maxsize,
                )
            except asyncio.QueueEmpty:  # pragma: no cover — full() just said otherwise
                pass
        await self._audio_queue.put((audio, sample_rate, epoch, enqueued_at))

    async def run(self):
        """Main recognition loop."""
        log.info("Recognition loop started.")
        while True:
            # CONC-4: the wait_for timeout — and ONLY it — means "no audio queued".
            # On Python 3.11 asyncio.TimeoutError IS builtins.TimeoutError (== a
            # socket timeout, and the base of aiohttp's ServerTimeoutError), so if
            # recognize()/_handle_result() sat inside this same try, a genuine
            # network timeout on the resolve/commit path would be misread as an
            # idle poll — swallowed with no log and retried immediately, hot-spinning
            # on a failing network with nothing in the journal. Keep that try around
            # the queue get alone.
            try:
                audio, sample_rate, epoch, enqueued_at = await asyncio.wait_for(
                    self._audio_queue.get(), timeout=self.poll_interval
                )
            except asyncio.TimeoutError:
                continue  # No audio queued within poll_interval — idle; poll again.

            # R10-11 (#424): if the dequeued head chunk is already STALE — older
            # than one capture hop (``_stale_after_seconds``), i.e. the consumer
            # has fallen behind — drain the rest of the backlog and keep only the
            # NEWEST chunk, so a post-stall resume acts on FRESH audio instead of
            # grinding oldest-first through ~40–50s of history.  enqueue() already
            # drops the OLDEST when the queue is FULL (#48); this is the
            # consumer-side complement, gated on staleness so it is a NO-OP in
            # steady state (a promptly-dequeued chunk has age ≈ 0 < one hop, so
            # back-to-back fresh chunks are all processed and two-hit confirmation
            # is unchanged).  The kept chunk still carries its OWN enqueue-time
            # epoch (PCONC-1), and the hard maxsize bound is untouched.  The final
            # policy (mailbox vs queue size vs this drain) is hardware-tuned on the
            # Pi — #424 stays open.
            age = time.monotonic() - enqueued_at
            dropped_stale = 0
            if age > self._stale_after_seconds:
                while True:
                    try:
                        audio, sample_rate, epoch, enqueued_at = (
                            self._audio_queue.get_nowait()
                        )
                        dropped_stale += 1
                    except asyncio.QueueEmpty:
                        break
                # Age of the chunk we will ACTUALLY recognize, after draining.
                age = time.monotonic() - enqueued_at
                # Queue-age telemetry (throttled, #178 model): surface the lag and
                # how many stale chunks were skipped so the #424 hardware tuning is
                # evidence-driven.  Never changes control flow.
                emit, suppressed = self._queue_lag_throttle.should_log(time.monotonic())
                if emit:
                    log.warning(
                        "Recognition queue lag: recognizing audio ~%.0fs old; "
                        "dropped %d stale backlog chunk(s) to resume on the "
                        "freshest audio (recognition slower than capture).%s",
                        age, dropped_stale,
                        (" %d further lag report(s) suppressed since the last."
                         % suppressed) if suppressed else "",
                    )

            # #454: per-track gating — skip the (costly) backend call while idling
            # between tracks. A fresh needle drop (new session epoch) still
            # recognizes at once; everything above (dequeue, staleness drain) has
            # already run, so capture never blocks.
            if not self._wants_recognition(epoch, time.monotonic()):
                continue

            try:
                # PCONC-2: bound the recognition call itself. Without this a
                # degraded shazamio call (its default retry is attempts=20 ×
                # max_timeout=60 — see ShazamIOBackend) can occupy the loop for
                # MINUTES over flaky wifi, saturating the maxsize-5 audio queue so
                # the consumer ends up working on 40–50s-old audio (the lag PCONC-1
                # needs). `recognize_timeout` is a DEDICATED bound, independent of
                # poll_interval so tuning the idle-poll rate can't accidentally cap
                # a real Shazam round-trip. On timeout wait_for raises TimeoutError,
                # handled below like any other error, and the next chunk retries.
                result = await asyncio.wait_for(
                    self.backend.recognize(audio, sample_rate),
                    timeout=self.recognize_timeout,
                )
                await self._handle_result(result, epoch)
                # A full recognize+commit turn succeeded — flush any loop-error
                # streak so its count is reported and the next error logs at once
                # (#197). Reached only when nothing above raised.
                self._loop_error_log.reset()
            except Exception as e:
                # Any error under the recognize/commit path — INCLUDING a genuine
                # TimeoutError (a raw socket timeout OR the wait_for above) — is
                # logged and backed off, not silently dropped. Use repr(e): a bare
                # TimeoutError stringifies to "" and would log an empty message,
                # hiding the exact flaky-wifi hang this bound exists to surface.
                # (asyncio.CancelledError is a BaseException, so a task cancel still
                # unwinds the loop cleanly.)
                # #197: throttled — a HUNG outage fires this every ~32s, 24/7; the
                # first error and any changed message still log immediately.
                self._loop_error_log.error(f"Recognition loop error: {e!r}")
                await asyncio.sleep(2)

    @staticmethod
    def _same_track(a: Optional[RawRecognitionResult], b: Optional[RawRecognitionResult]) -> bool:
        """Compare two recognition results case- and whitespace-insensitively.

        Shazam occasionally returns subtly different formatting for the same
        track between chunks (trailing whitespace, capitalization tweaks).
        Without normalization those count as a new track and trigger an
        unnecessary re-resolve / re-scrobble.

        R7-04 (accepted cost, deliberate): the comparison uses title + artist
        ONLY, never the album.  Shazam's album field is unstable between chunks
        for one track (it reports a track's ORIGINAL album, a comp, a reissue —
        the R5-07 note), so comparing album would flip a still-playing track to
        "new" every time the field wobbled, forcing per-chunk re-resolve /
        re-scrobble churn (the worse failure).  The cost of excluding it: a
        genuine RECORD CHANGE whose boundary track shares the previous track's
        title AND artist (a live vs. studio "Untitled", a self-titled track, a
        common cover across two owned records) compares equal here, so the new
        record's opener is swallowed — no commit, no scrobble, one supporting
        row lost, and the card stays on the old track until the NEXT,
        differently-named track arrives.  Reproduced 2026-08-11; kept as-is
        because re-including the unstable album field trades a rare, self-healing
        miss for frequent churn on every album-tier resolve.
        """
        if a is None or b is None:
            return False

        # REC-4: normalize whitespace-insensitively (as the docstring promises),
        # not just at the ends. Shazam returns subtly different INTERNAL spacing
        # for the same track between chunks ("My  Song" vs "My Song"); `.strip()`
        # alone left those comparing unequal, forcing a needless re-resolve /
        # re-scrobble. `" ".join(s.split())` collapses every run of whitespace
        # (and trims the ends), and `.casefold()` is the Unicode-aware
        # case-fold (stronger than `.lower()`) for the comparison.
        # REC-2: `… or ""` guards a None title/artist defensively — the parser now
        # coerces both (title via REC-3, artist here), but a None slipping in from
        # any future source must compare as empty, never crash the dedup.
        def _norm(s: Optional[str]) -> str:
            # R5-10: str()-coerce so a non-string value (a numeric field that
            # slipped past the parser) can never AttributeError here — the parser
            # already coerces, this is the defense-in-depth backstop.
            return " ".join(str(s or "").split()).casefold()

        return (
            _norm(a.title) == _norm(b.title)
            and _norm(a.artist) == _norm(b.artist)
        )

    async def _handle_result(self, result: Optional[RawRecognitionResult], epoch: int = 0):
        """Apply confirmation logic, then resolve metadata and update state.

        ``epoch`` is the session epoch bound to *this* chunk's audio at enqueue
        time (PCONC-1).  On a real confirmation it is forwarded to
        ``on_confirmed`` so the commit is validated against the session the
        audio came from — not whichever session happens to be live when the
        (possibly queue-delayed) commit finally runs.

        The ``epoch=0`` default is a TEST-ONLY affordance: the many
        confirmation-logic tests that drive ``_handle_result`` directly do not
        care about epochs, and 0 is the initial epoch so they behave identically.
        ⚠️  Any NEW production caller MUST pass the real bound epoch — ``run()``
        is currently the only one, and it does.  A wrong/stale epoch here cannot
        cause a phantom write: the commit boundary takes an explicit REQUIRED
        ``audio_epoch`` and fails safe, so the worst case is the guard discarding
        live commits (loud missed identifications), never a silent bad write.
        """
        if _DEBUG_RECOGNITION:  # #472
            log.info(
                "recognition-debug poll: result=%s off=%s status=%s cur=%s pending=%s x%d",
                (f"{result.artist} — {result.title}" if result else None),
                getattr(result, "match_offset", None),
                self.state.status.name,
                (self.state.current_raw.title if self.state.current_raw else None),
                (self._pending_result.title if self._pending_result else None),
                self._pending_count,
            )
        # PCONC-3: on a session boundary (the epoch changed since the last chunk),
        # reset the per-session HEALTH counters. `_miss_count` gates the LISTENING
        # "NO MATCH FOUND" screen and `_churn_count` the churn breadcrumb; both are
        # about THIS side's recognition, so a streak inherited from the previous
        # side would surface ERROR (or log churn) on fewer of the new side's own
        # chunks. The pending candidate is voided separately just below (REC-1),
        # which also handles a stale SAME-epoch pending; this only adds the health
        # counters. REC-1's accumulate-across-misses is WITHIN a session (constant
        # epoch), so it is untouched.
        if epoch != self._last_epoch:
            self._miss_count = 0
            self._churn_count = 0
            self._last_epoch = epoch

        # REC-1 review: void the pending candidate across a SESSION boundary. The
        # pending (result + count) lives on this loop, not the session; a needle
        # lift ends the session and bumps `session_epoch` (clear()), and — now that
        # a miss no longer zeroes the pending (below) — a leftover count would
        # otherwise let a SINGLE spurious hit of the previous record confirm it into
        # the NEW record's session (a phantom now-playing card + play count +
        # scrobble the epoch guard can't catch, because the confirming audio is
        # genuinely live). A chunk whose epoch differs from the pending's belongs to
        # a different session, so the stale pending is discarded. Within a session
        # the epoch is constant, so REC-1's accumulate-across-misses is unaffected.
        # (`_pending_epoch` is intentionally NOT cleared here — it is only ever
        # read while `_pending_result is not None`, and the sole site that sets
        # the pending non-None re-tags the epoch on the same pass, so a stale
        # epoch beside a None pending is inert.)
        if self._pending_result is not None and epoch != self._pending_epoch:
            self._pending_result = None
            self._pending_count = 0
            self._confirm_attempts = 0

        if result is None:
            # REC-1: a None (unrecognized-audio) result carries NO recognition
            # information — it must NOT discard the pending candidate. On vinyl,
            # hit/miss/hit/miss is the normal failure mode (surface noise, a worn
            # side); zeroing the pending here meant a track Shazam identified every
            # OTHER chunk could never reach confirmation_required consecutive
            # matches, so it never committed and _register_miss eventually latched
            # the display to ERROR ("NO MATCH FOUND"). Leaving the pending
            # untouched lets alternating matches of the SAME track still accumulate
            # to a confirmation — a DIFFERENT non-None result still replaces the
            # pending below, so genuine churn is unaffected. The miss still counts
            # toward the ERROR threshold, but a real alternating identification now
            # confirms first, so ERROR fires only when the side is genuinely
            # unrecognizable (and a later reappearance still recovers).
            self._register_miss()
            if self._pending_count > 0:
                self._confirm_attempts += 1
                if self._confirm_attempts >= _CANDIDATE_CONFIRM_ATTEMPTS:
                    self._void_stale_candidate(time.monotonic())
            return

        if self._same_track(result, self.state.current_raw):
            self._miss_count = 0  # same track still playing — recognition works (B-7)
            self._churn_count = 0  # …and not churning (B-21)
            # R5-04: a hit on the CURRENT track is positive evidence AGAINST any
            # half-accumulated competitor, so discard the pending candidate here.
            # Without this, a single stray misrecognition of B left B pending for
            # the rest of the side (misses deliberately don't clear it, REC-1),
            # and one more isolated B hit — even 20 correct A chunks later —
            # reached confirmation_required and committed the wrong track. This is
            # NOT the REC-1 case: REC-1 only requires that a None (miss) not clear
            # the pending, preserving hit/miss/hit accumulation of the SAME track;
            # a confirmed different current track is a stronger signal than a lone
            # stale competitor and legitimately resets it.
            was_building = self._pending_count > 0
            self._pending_result = None
            self._pending_count = 0
            self._confirm_attempts = 0
            # #454: same track still playing at reactivation (prediction early /
            # turntable slow) — re-idle a short beat. If a NEW-track candidate was
            # mid-confirmation, a stray current-track hit shouldn't send us idle
            # (cold-review LOW) — keep recognizing to confirm the change.
            if not was_building:
                self._reidle_same_track(time.monotonic())
            return  # Same track still playing

        if self._same_track(result, self._pending_result):
            self._pending_count += 1
        else:
            self._pending_result = result
            self._pending_count = 1
        self._pending_epoch = epoch  # tag the pending with its session (REC-1 review)

        if self._pending_count >= self.confirmation_required:
            log.info(f"Track confirmed: {result.artist} — {result.title}")
            self._miss_count = 0  # a real commit — recognition works (B-7)
            self._churn_count = 0  # …and not churning (B-21)
            # Hand the confirmed result to the commit service (A-9), along with
            # the epoch this audio was captured under (PCONC-1).  We await it so
            # the next chunk isn't processed until current_raw has been advanced
            # (the dedup at the top depends on that ordering).
            await self.on_confirmed(result, epoch)
            self._pending_result = None
            self._pending_count = 0
            self._confirm_attempts = 0
            # #454: track confirmed — idle recognition until the predicted next
            # boundary (reads the just-committed state.current_track duration).
            self._go_idle_until_boundary(result, time.monotonic())
        else:
            # A non-None result that neither matches the current track nor (yet)
            # confirms — unconfirmable churn (a noisy room, two records bleeding
            # together).  Count it toward ERROR so the display doesn't spin on
            # the boot/IDENTIFYING screen forever.  Previously _miss_count was
            # reset on EVERY non-None result, so neither churn nor interspersed
            # None-misses could ever accumulate to surface ERROR (B-7).
            self._churn_count += 1
            if self._churn_count % _CHURN_LOG_EVERY == 0:
                # Diagnostic breadcrumb (B-21): the display looks "stuck" not
                # because recognition failed but because it keeps seeing
                # different tracks that never confirm.  Conservative by design —
                # we still don't guess — but now the journal says why.
                log.warning(
                    "Recognition churning: %d consecutive unconfirmable results "
                    "(latest: %s — %s); display not updated.",
                    self._churn_count, result.artist, result.title,
                )
            self._register_miss()
            self._confirm_attempts += 1
            if self._confirm_attempts >= _CANDIDATE_CONFIRM_ATTEMPTS:
                self._void_stale_candidate(time.monotonic())

    def _current_track_duration_seconds(self) -> Optional[float]:
        """Duration (seconds) of the currently-committed track from its Discogs
        tracklist row, or None when unknown (no track, unmatched row, a row with no
        duration string, or any malformed metadata — all fall back to the safety
        timer)."""
        try:
            track = self.state.current_track
            if track is None:
                return None
            idx = track.side_index.global_index
            if idx is None or not (0 <= idx < len(track.tracklist)):
                return None
            return _parse_mmss(track.tracklist[idx].duration)
        except Exception:
            return None

    def _go_idle_until_boundary(self, result: "RawRecognitionResult", now: float) -> None:
        """A track just confirmed — idle recognition until the predicted next-track
        boundary (Discogs duration minus the AudD match offset), or the safety
        re-check interval when no duration is available (#454)."""
        duration = self._current_track_duration_seconds()
        if duration is not None:
            remaining = duration - (result.match_offset or 0.0)
            wait = max(remaining, _MIN_REACTIVATE_SECONDS) + _BOUNDARY_MARGIN_SECONDS
        else:
            wait = self._safety_recheck_seconds
        # #454 (cold-review MEDIUM): never idle longer than the safety interval, so a
        # garbage Discogs duration ("99:00" -> 99 min) can't freeze the display for a
        # whole side; the display lag is universally bounded.
        wait = min(wait, self._safety_recheck_seconds)
        if _DEBUG_RECOGNITION:  # #472
            log.info(
                "recognition-debug idle: dur=%s off=%s wait=%.1fs (until next-track boundary)",
                duration, (result.match_offset or 0.0), wait,
            )
        self._reactivate_at = now + wait
        self._recognition_active = False

    def _reidle_same_track(self, now: float) -> None:
        """Reactivation found the same track still playing — re-idle a short beat
        rather than recognizing every hop until it actually changes (#454)."""
        self._reactivate_at = now + _SAME_TRACK_RECHECK_SECONDS
        self._recognition_active = False

    def _back_off_after_error(self, now: float) -> None:
        """NO MATCH FOUND — back recognition off so an unrecognizable side can't
        hammer the backend, but only to the SHORT ERROR retry (#460), not the long
        between-known-tracks safety interval: 240s exceeds a track, so it could never
        accumulate the two consecutive matches needed to confirm a later track."""
        self._reactivate_at = now + _ERROR_RETRY_SECONDS
        self._recognition_active = False

    def _void_stale_candidate(self, now: float) -> None:
        """#464: a candidate never got its confirming second hit within the fast-poll
        burst (the track ended, or it was a stray/one-off misrecognition) — discard it
        and resume the slow back-off, so a stranded pending can't fast-poll forever."""
        self._pending_result = None
        self._pending_count = 0
        self._confirm_attempts = 0
        self._reactivate_at = now + _ERROR_RETRY_SECONDS
        self._recognition_active = False

    def _wants_recognition(self, epoch: int, now: float) -> bool:
        """Whether to run the (costly) backend on the just-dequeued chunk (#454).

        A new session (needle drop → epoch bump) always recognizes immediately —
        even mid-idle — so a fresh record is identified at once. Otherwise, while
        idling between tracks, recognize only once the reactivation time arrives.
        """
        if epoch != self._last_seen_session_epoch:
            self._last_seen_session_epoch = epoch
            self._recognition_active = True
        # #454 (cold-review HIGH): LISTENING means no track is up yet — always try,
        # so a needle reposition out of ERROR/IDLE recovers at once (its brief
        # silence does NOT bump session_epoch), instead of waiting out the idle timer.
        if self.state.status == PlayerStatus.LISTENING:
            self._recognition_active = True
        # #464: a candidate is mid-confirmation — poll at the fast chunk rate,
        # ignoring the ERROR/idle back-off timer, so its confirming second hit is
        # caught while the same track is still playing. Bounded by _confirm_attempts
        # in _handle_result (a stale candidate is voided), so this can't hammer.
        if self._pending_count > 0:
            self._recognition_active = True
        if not self._recognition_active and now >= self._reactivate_at:
            self._recognition_active = True
        return self._recognition_active

    def _register_miss(self):
        """Count a failed recognition; surface ERROR after enough of them.

        Misses only matter while LISTENING — before the first successful
        identification.  During PLAYING, surface noise and quiet passages
        produce routine misses that mean nothing; in IDLE there's no needle
        down; in ERROR we're already showing the failure.  ERROR is recovered
        by repositioning the needle (silence → music re-enters LISTENING) or
        by a successful commit (set_track → PLAYING).
        """
        if self.state.status == PlayerStatus.LISTENING:
            self._miss_count += 1
            if self._miss_count >= self.error_after_misses:
                log.info(
                    "Recognition failed %d consecutive times while listening — "
                    "showing NO MATCH FOUND.", self._miss_count,
                )
                self._miss_count = 0
                self.state.set_status(PlayerStatus.ERROR)
                # #454: back off after NO MATCH FOUND so an unrecognizable side
                # can't hammer the backend; a needle move / the safety timer retries.
                self._back_off_after_error(time.monotonic())
        else:
            self._miss_count = 0
            # #454 (cold-review MEDIUM): a miss while ERROR is latched must re-back-off,
            # not fall through to recognizing every hop — each still-unmatched wake
            # from the safety timer re-idles again (bounded requests, not a flood).
            if self.state.status == PlayerStatus.ERROR:
                self._back_off_after_error(time.monotonic())
