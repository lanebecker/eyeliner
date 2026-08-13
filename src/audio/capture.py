"""Audio capture from USB audio interface.

Records continuously from the configured sounddevice input and feeds
genuinely overlapping chunks into the silence detector and recognition loop.

Capture design (v1.3.3)
-----------------------
Earlier versions used blocking sd.rec() calls separated by a sleep, which
left a dead gap between chunks (see src/audio/chunking.py for the full
story).  Capture now works in three stages:

  1. sd.InputStream records continuously; its PortAudio callback (which runs
     on a non-asyncio audio thread) hands each ~0.25s block to the event loop
     via loop.call_soon_threadsafe.
  2. run() drains those blocks from an asyncio.Queue and feeds them to a
     ChunkAssembler, which emits a chunk_seconds-long window every
     (chunk_seconds - overlap_seconds).
  3. Each emitted chunk goes synchronously to SilenceDetector.process() and
     asynchronously to RecognitionLoop.enqueue() — same consumers, same
     chunk shape as before; only the windowing changed.

If the block queue ever fills (the event loop stalls for >16s), the OLDEST
block is dropped and a warning logged — recent audio wins.
"""

import asyncio
import logging
import time
from typing import Optional, TYPE_CHECKING

import numpy as np
import sounddevice as sd

from src.audio.chunking import ChunkAssembler
from src.util.logthrottle import LogThrottle

if TYPE_CHECKING:
    from src.audio.silence import SilenceDetector
    from src.audio.recognizer import RecognitionLoop
    from src.config import AudioConfig

log = logging.getLogger(__name__)

# Size of each InputStream callback block, in seconds.  Small enough to keep
# silence-detection latency low, large enough that call_soon_threadsafe runs
# only ~4×/second.
_BLOCK_SECONDS = 0.25

# Max blocks buffered between the audio callback and run().  64 × 0.25s = 16s
# of slack before the drop-oldest policy kicks in.
_BLOCK_QUEUE_MAX = 64

# How often the silence ticker re-evaluates the end-of-session timer when no
# audio chunks are arriving (B-6).  The session-end threshold is tens of
# seconds, so 1s granularity is plenty and the cost (one comparison) is trivial.
_SILENCE_TICK_SECONDS = 1.0

# PCONC-4: minimum wall-clock gap between drop-oldest warnings. The overflow
# fires once per dropped block (4×/s per stalled second), and one stalled loop
# turn was measured emitting 53 WARNING records — an SD-card log flood. So the
# drops are COUNTED and surfaced as at most one summarizing warning per this
# interval: a sustained-backlog health signal, not a per-event log.
_DROP_WARN_INTERVAL_SECONDS = 5.0

# CONC-5: how long run() waits for a block before deciding the stream is DEAD.
# Blocks arrive ~every _BLOCK_SECONDS (4×/s) on a healthy stream, so a gap of
# 16 blocks' worth (4s) means the PortAudio callback has stopped firing — the
# device browned out / was unplugged, or the callback aborted from CFFI — and
# no exception surfaced in the consumer. Generous enough that a transient
# event-loop hiccup can't false-trip it, short enough to recover promptly.
_BLOCK_STALL_TIMEOUT_SECONDS = 16 * _BLOCK_SECONDS

# Backoff before rebuilding the stream after a capture error (a construction
# failure OR a detected stall). Named so tests can drive the retry fast.
_STREAM_RETRY_BACKOFF_SECONDS = 1.0

# #178: minimum wall-clock gap between capture-loop error logs. A PERMANENT
# failure (a misconfigured device_name that never matches, or a device absent
# forever) raises every retry — at ~1 error per _STREAM_RETRY_BACKOFF_SECONDS
# that would flood the journal/SD card (the PCONC-4 class, on the error path).
# The first error, and any error whose message CHANGES, still logs immediately;
# identical repeats are counted and summarized at most once per this interval.
_CAPTURE_ERROR_WARN_INTERVAL_SECONDS = 30.0

# R6-28: minimum wall-clock gap between PortAudio-status warnings (input overflow /
# underflow etc.). A persistent status flag is raised on EVERY callback (~4×/s), and
# the flag most often means the loop can't keep up — so logging (blocking handler
# I/O) ON the realtime callback thread can itself worsen the overrun. The status is
# marshalled onto the event loop and throttled there, per distinct flag string.
_AUDIO_STATUS_WARN_INTERVAL_SECONDS = 30.0


class AudioCapture:
    """Wraps sounddevice to stream overlapping audio chunks from the USB interface."""

    def __init__(self, config: "AudioConfig", silence: "SilenceDetector", recognizer: "RecognitionLoop"):
        self.silence = silence
        self.recognizer = recognizer
        self._running = False
        self._blocks: Optional[asyncio.Queue] = None

        # All four always-on log sites below share ONE throttle (arch-5/#221,
        # src/util/logthrottle.py) instead of hand-rolling the -inf-seed pattern
        # four times; the seed subtlety now lives in the LogThrottle docstring.

        # PCONC-4: drop-oldest warning — a keyless interval summarizer (the message
        # never changes, only its count), so bursts collapse into one throttled
        # health signal per _DROP_WARN_INTERVAL_SECONDS.
        self._drop_throttle = LogThrottle(interval=_DROP_WARN_INTERVAL_SECONDS)

        # #178: capture-loop error log — per_message summarizer. R6-02: per_message
        # (not single-key) so a device flapping between TWO failure shapes
        # (~1 rebuild/s) can't defeat the throttle — in single-key mode every
        # message change, including changing back, emits, so alternating errors
        # flood the journal/SD card (the class R5-13 fixed for the recognizer
        # sites). Each key is rate-limited on its own interval and carries its own
        # tally. (Depends on R6-01: per_message reset() must not corrupt the key
        # map — this site never calls reset(), but the mode's correctness rests on
        # that fix.)
        # #304 (R6-02 cold-review F1/F2 follow-up, FIXED): _log_capture_error keys
        # this on the error CLASS (type name), NOT the full message. A message-keyed
        # throttle grew an unbounded per-key map as a varying detail minted a new
        # key per variant, and — this raw throttle having no recovery point — a
        # since-stopped variant's suppressed count was buried or LRU-evicted
        # unsurfaced. The exception type is a bounded key set, so nothing is ever
        # evicted/buried; anti-flood is preserved (a few throttled keys, not one
        # emit per message change).
        self._capture_error_throttle = LogThrottle(
            interval=_CAPTURE_ERROR_WARN_INTERVAL_SECONDS, per_message=True
        )

        # #164: the device lookup runs on every stream rebuild (see run()), so its
        # two logs are deduped (interval=None → pure dedup, never periodic re-warn)
        # to avoid re-emitting every iteration during a sustained rebuild/stall
        # loop. The two signals key on DIFFERENT things: the "using device" INFO on
        # the winning index (a re-plug to a new index re-logs — exactly the signal
        # to surface); the multi-match WARNING on the full match SET, so a config
        # that becomes NEWLY ambiguous (a second matching device appears) still
        # warns even when the winning index is unchanged.
        self._device_using_throttle = LogThrottle()
        self._device_match_throttle = LogThrottle()
        # R8-20 (#369): warn ONCE when the sounddevice private-API device-table
        # refresh (#194 hotplug recovery) turns out to be unavailable.
        self._device_refresh_degraded_warned = False

        # R6-28: PortAudio input-status warning, marshalled off the realtime
        # callback thread onto the loop and throttled per distinct flag string so a
        # persistent overflow can't write ~4 journal lines/second (and can't add
        # blocking log I/O to the realtime thread and worsen the overrun).
        self._status_throttle = LogThrottle(
            interval=_AUDIO_STATUS_WARN_INTERVAL_SECONDS, per_message=True
        )

        self.sample_rate: int = config.sample_rate
        self.chunk_seconds: int = config.chunk_seconds
        self.overlap_seconds: int = config.overlap_seconds
        self.device_name: str = config.device_name

        # Guard against a misconfigured overlap: hop must stay >= 1 frame.
        # overlap >= chunk would mean each chunk advances zero (or negative)
        # frames — an infinite re-recognition of the same audio.
        if self.overlap_seconds >= self.chunk_seconds:
            log.warning(
                f"audio.overlap_seconds ({self.overlap_seconds}) >= "
                f"chunk_seconds ({self.chunk_seconds}); disabling overlap. "
                f"Fix config.yaml — overlap must be smaller than the chunk."
            )
            self.overlap_seconds = 0

    def _find_device_index(self) -> int:
        """Look up the sounddevice index for the configured device name.

        Matching is case-insensitive substring against the device name.  If
        more than one input device matches, the first is used but ALL matches
        are logged — multi-USB-audio setups (e.g. UCA222 + a USB mic) can be
        diagnosed from the logs without having to guess which one got picked.
        """
        devices = sd.query_devices()
        matches = [
            (i, device) for i, device in enumerate(devices)
            if (
                self.device_name.lower() in device["name"].lower()
                and device["max_input_channels"] > 0
            )
        ]
        if matches:
            i, device = matches[0]
            # Multi-match WARNING: keyed on the full match SET so it re-warns when
            # the ambiguity itself changes (a newly-matching device appears), not
            # only when the winner moves — but stays quiet across a rebuild loop
            # that keeps seeing the same set (#164).
            now = time.monotonic()  # unused by these dedup throttles (interval=None)
            match_key = tuple(idx for idx, _ in matches)
            # Call should_log UNCONDITIONALLY so the match throttle's key advances
            # on EVERY observation (#164: a set that drops to 1-match then becomes
            # ambiguous again must re-warn) — gate the emit on len>1 afterwards.
            match_emit, _ = self._device_match_throttle.should_log(now, key=match_key)
            if len(matches) > 1 and match_emit:
                others = ", ".join(f"[{j}] {d['name']}" for j, d in matches[1:])
                log.warning(
                    f"Multiple input devices match '{self.device_name}'. "
                    f"Using the first; others were: {others}. "
                    f"Tighten audio.device_name in config.yaml if this is wrong."
                )
            # "Using device" INFO: keyed on the winning index, so a re-plug to a
            # different index re-logs while a stable device stays quiet (#164).
            using_emit, _ = self._device_using_throttle.should_log(now, key=i)
            if using_emit:
                log.info(f"Using audio device [{i}]: {device['name']}")
            return i
        available = [d["name"] for d in devices if d["max_input_channels"] > 0]
        raise ValueError(
            f"Audio device '{self.device_name}' not found. "
            f"Available input devices: {available}"
        )

    def _refresh_audio_devices(self) -> None:
        """Re-enumerate PortAudio's device table on the rebuild loop's FAILURE
        path so a device that appeared or moved after import can be found (#194).

        PortAudio snapshots the device table ONCE, at Pa_Initialize() — which
        python-sounddevice runs at ``import sounddevice`` (sd 0.5.5), long before
        this loop. ``query_devices()`` only iterates that frozen table; PortAudio
        never rescans. So a device absent when the service started (the UCA222
        still enumerating over USB while systemd — ordered only on network.target,
        CRIT-4/#83 — brings us up), or one re-plugged to a different ALSA card
        index mid-run, can NEVER appear to ``_find_device_index()`` no matter how
        many times the loop retries: capture becomes an alive-but-idle zombie
        until a human restarts the process. #164's in-loop re-resolution promised
        this recovery but could not deliver it against a static table — the unit
        test only passed because its ``query_devices`` mock returned a different
        list on the second call, exactly what the real library cannot do.

        ``_terminate()`` + ``_initialize()`` is python-sounddevice's documented
        rescan recipe: it tears PortAudio down and re-runs Pa_Initialize(),
        rebuilding the table so the NEXT ``_find_device_index()`` sees the current
        hardware.

        Safety and cost:
          * Call ONLY from the rebuild loop's failure handler, where the
            ``with stream:`` block has already exited and NO stream object is
            live — ``_terminate()`` while a stream is open is undefined behaviour.
          * Both are PRIVATE sounddevice APIs (pinned: 0.5.5). The whole thing is
            wrapped: if an upstream refactor removes or breaks them, we log once at
            debug and DEGRADE to the pre-#194 behaviour (retry against the existing
            table) rather than turning the recovery path into a new crash loop.
          * The rescan is synchronous (ALSA re-enumeration, up to a few hundred
            ms) and runs on the event loop. Acceptable at the 1s retry cadence —
            during a device-down retry the display is parked on IDLE/ERROR anyway,
            and the independent _silence_ticker only needs ~1s granularity.
        """
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:
            # Degrade to pre-#194 behaviour: the next retry simply sees the table
            # it would have seen anyway. A private-API breakage must not escalate a
            # recoverable device-down loop into a crash.
            #
            # R8-20 (#369): the refresh rests on sounddevice PRIVATE APIs
            # (`sd._terminate`/`sd._initialize`, pinned working at the 0.5.5
            # floor) — a future `pip install -U sounddevice` on the Pi could
            # remove them and silently revert the #194 hotplug recovery.  Say
            # so ONCE at WARNING (with the installed version) so a bring-up
            # journal shows the degradation; repeats stay debug (the real
            # failure is already surfaced by _log_capture_error just above).
            # Known-accepted (2nd review): the warn is once-per-PROCESS on ANY
            # exception here — a one-off transient during re-init consumes it,
            # and a later genuine API removal then logs only at debug.  The
            # exception repr in the message keeps the journal diagnosable
            # either way; a per-cause latch isn't worth the state.
            if not self._device_refresh_degraded_warned:
                self._device_refresh_degraded_warned = True
                log.warning(
                    "PortAudio device-table refresh unavailable (%r; "
                    "sounddevice %s) — the #194 hotplug recovery is degraded: "
                    "a re-plugged device may need a service restart to be "
                    "seen (R8-20).", e, getattr(sd, "__version__", "?"),
                )
            else:
                log.debug(
                    "PortAudio device-table refresh unavailable (%r); retrying "
                    "against the existing table.", e
                )

    def _enqueue_block(self, blocks: asyncio.Queue, block: np.ndarray):
        """Put an audio block on the queue, dropping the OLDEST first when it's
        full so recent audio wins — the drop-oldest overflow policy the module
        docstring sells as a correctness feature.

        Runs on the event-loop thread (scheduled by the callback via
        call_soon_threadsafe), never on the PortAudio audio thread.
        """
        if blocks.full():
            try:
                blocks.get_nowait()  # Drop the OLDEST block — recent audio wins
            except asyncio.QueueEmpty:  # pragma: no cover — full() just said otherwise
                pass
            else:
                # PCONC-4: count every drop, but WARN at most once per
                # _DROP_WARN_INTERVAL_SECONDS with the aggregate since the last
                # report — so a stalled loop dropping 4 blocks/s can't flood the
                # journal (53 records in one turn was measured). The first drop
                # after a quiet spell reports immediately (`_last_drop_warn`
                # starts at -inf); a sustained backlog then reports periodically.
                emit, suppressed = self._drop_throttle.should_log(time.monotonic())
                if emit:
                    log.warning(
                        "Audio block queue full; dropped %d block(s) since the "
                        "last report — the event loop is stalling (recent audio "
                        "wins).",
                        suppressed + 1,   # +1: this drop counts too (PCONC-4 aggregate)
                    )
        blocks.put_nowait(block)

    def _make_callback(self, loop: asyncio.AbstractEventLoop, blocks: asyncio.Queue):
        """Build the InputStream callback (runs on the PortAudio audio thread).

        The callback must never touch asyncio objects directly — it marshals
        each block onto the event loop with call_soon_threadsafe, where
        _enqueue_block applies the drop-oldest overflow policy.
        """
        def callback(indata, frames, time_info, status):
            try:
                if status:
                    # R6-28: do NOT log on this realtime thread — marshal the status
                    # onto the loop, where _log_audio_status throttles it. Blocking
                    # handler I/O here can itself cause further overflows (feedback).
                    loop.call_soon_threadsafe(self._log_audio_status, str(status))
                # Copy: PortAudio reuses the indata buffer after the callback returns.
                block = indata[:, 0].copy()
                loop.call_soon_threadsafe(self._enqueue_block, blocks, block)
            except Exception as e:
                # CONC-5: a raising callback aborts the PortAudio stream from CFFI
                # with NO exception surfacing in run(), so capture would silently
                # die. Swallow + log so the stream keeps running; a genuinely dead
                # stream is then caught by the block-stall timeout in run().
                log.error("Audio callback error: %s", e, exc_info=True)  # R5-26

        return callback

    @staticmethod
    def _capture_error_key(error: Exception) -> str:
        """R8-12 (#358): the throttle key — exception type + the first WORD of
        the message (with a leading "[Errno N]" bracket stripped first).

        The #304 type-only key over-coarsened: two DIFFERENT failure conditions
        of the same class ("Device unavailable" then "Invalid sample rate", both
        OSError) shared one key, so a genuinely NEW condition stayed invisible
        for up to a full 30s window and the eventual summary attributed a MIXED
        tally to whichever message was current.  The condition word distinguishes
        them while staying a small bounded set — unlike full-message keying (the
        #304 bug), a varying detail (device index, errno, PaErrorCode) cannot
        mint per-variant keys.

        Shape handling (W3 cold-review F3 — verified against the installed
        sounddevice 0.5.5 `_check`): sounddevice's ``PortAudioError`` formats as
        ``"Error opening InputStream: Device unavailable [PaErrorCode -9985]"``
        — a CONSTANT first word ("Error"), the condition in the LAST colon
        segment, and a trailing bracket.  A plain ``OSError`` formats as
        ``"[Errno -9997] Invalid sample rate"`` — a leading bracket.  So: strip
        a leading "[…]", strip a trailing "[…]", then key on the first word of
        the LAST ": "-separated segment.  Without the shape handling the
        dominant capture error class degenerated back to type-only keying.
        """
        msg = str(error)
        if msg.startswith("[") and "]" in msg:
            msg = msg.split("]", 1)[1].strip()   # "[Errno -9997] Invalid…" → "Invalid…"
        # The last-colon-segment rule applies ONLY to the trailing-bracket
        # (sounddevice) shape: there the segment before the bracket is the
        # CONDITION.  Applying it unconditionally would key an OSError like
        # "Device unavailable: hw:5" on its varying DETAIL ("hw:5") — minting
        # per-variant keys, the exact #304 bug.
        if msg.endswith("]") and "[" in msg:
            msg = msg[: msg.rfind("[")].strip()  # "…unavailable [PaErrorCode -9985]" →
            if ": " in msg:                      # "Error opening InputStream: Device…"
                msg = msg.rsplit(": ", 1)[1].strip()   # → "Device unavailable"
        first = msg.split(maxsplit=1)[0] if msg else ""
        return f"{type(error).__name__}:{first}"

    def _log_capture_error(self, error: Exception) -> None:
        """Log a capture-loop error, throttled so a PERMANENT failure can't flood
        the journal (#178).

        The first error of each CONDITION reports immediately (R8-12: keyed on
        exception type + first message word, see ``_capture_error_key``);
        further errors of that condition are counted and summarized at most
        once per _CAPTURE_ERROR_WARN_INTERVAL_SECONDS, so a device that is
        misconfigured or absent forever leaves a periodic health line, not one
        record per backoff.  The full message is always shown on the emitted line.
        """
        msg = str(error)
        emit, suppressed = self._capture_error_throttle.should_log(
            time.monotonic(), key=self._capture_error_key(error)
        )
        if not emit:
            return
        if suppressed > 0:
            log.error(
                "Audio capture error: %s (%d further error(s) suppressed since "
                "the last report)", msg, suppressed,
            )
        else:
            log.error("Audio capture error: %s", msg)

    def _log_audio_status(self, status_str: str) -> None:
        """Log a PortAudio input-status flag, throttled (R6-28). Runs ON the event
        loop (marshalled from the realtime callback via call_soon_threadsafe), so
        the LogThrottle — which is not thread-safe — is only ever touched here."""
        emit, suppressed = self._status_throttle.should_log(
            time.monotonic(), key=status_str
        )
        if not emit:
            return
        if suppressed > 0:
            log.warning(
                "Audio input status: %s (%d further occurrence(s) suppressed since "
                "the last report)", status_str, suppressed,
            )
        else:
            log.warning("Audio input status: %s", status_str)

    async def _silence_ticker(self):
        """Periodically poke the SilenceDetector so the end-of-session timer is
        evaluated even when no audio chunks are arriving (B-6).

        process() only runs on chunk arrival, so a stall during silence (an
        InputStream error parking run() in its retry sleep, or a drained block
        queue) would otherwise leave a completed album's SESSION_ENDED unfired
        and its Play Count never credited.  This task ticks on wall-clock time
        independently of chunk flow.
        """
        while self._running:
            await asyncio.sleep(_SILENCE_TICK_SECONDS)
            try:
                self.silence.tick()
            except Exception as e:
                # A listener raising must not kill the ticker — that would
                # permanently disable the session-end safety net this task
                # exists to provide.  (CancelledError is BaseException and is
                # intentionally NOT caught, so shutdown still propagates.)
                log.error("Silence ticker tick failed: %s", e, exc_info=True)  # R5-26

    async def _dispatch_chunk(self, chunk: np.ndarray, sample_rate: int):
        """Classify one chunk and dispatch it for recognition ONLY while music
        is playing (#193/#195).

        Silence detection runs on EVERY chunk (sync, one RMS) because it drives
        the whole session lifecycle — MUSIC_STARTED / MUSIC_STOPPED / SESSION_ENDED.
        Recognition, by contrast, is gated on the detector's music verdict: an
        idle turntable otherwise POSTs digitally-silent audio to Shazam's
        unofficial API every hop, ~8,640×/day forever (#193 — pure waste plus a
        throttle/block risk that would break recognition when music DOES play).
        Gating here also closes #195 structurally: a tracker session can only
        start off a recognized track, so it can only start while the detector is
        in music-state — which guarantees the music→silence transition that arms
        SESSION_ENDED is reachable, so a session can never become immortal. The
        low-gain case (audible music below the RMS threshold) is treated as
        silence and surfaced by SilenceDetector's throttled low-gain warning
        rather than tracked. `is_music_playing` reflects the state AFTER this
        chunk was processed (SIL-4 hysteresis), so the first music chunk is
        recognized and the first fully-silent chunk is not.
        """
        self.silence.process(chunk, sample_rate)
        if self.silence.is_music_playing:
            # Recognition enqueue never blocks (drops when full).
            await self.recognizer.enqueue(chunk, sample_rate)

    async def run(self):
        """Main capture loop. Streams audio and dispatches overlapping chunks."""
        loop = asyncio.get_running_loop()

        # int() guards against fractional seconds in config.yaml (v1.3.5):
        # float frame counts previously sailed through to numpy slicing and
        # crashed mid-capture with a cryptic TypeError.  ChunkAssembler also
        # validates integrality as a second line of defence.
        chunk_frames = int(self.chunk_seconds * self.sample_rate)
        hop_frames = int((self.chunk_seconds - self.overlap_seconds) * self.sample_rate)
        self._running = True

        log.info(
            f"Starting audio capture: {self.chunk_seconds}s chunks "
            f"at {self.sample_rate}Hz, new chunk every "
            f"{self.chunk_seconds - self.overlap_seconds}s "
            f"({self.overlap_seconds}s overlap)"
        )

        # Independent timer tick so SESSION_ENDED fires on wall-clock time even
        # while the stream is down and no chunks flow (B-6).
        ticker = asyncio.create_task(self._silence_ticker())
        try:
            while self._running:
                # Clear any stuck "music playing" flag from a previous stream
                # so recovered audio re-emits MUSIC_STARTED (B-6).
                self.silence.reset_music_state()
                assembler = ChunkAssembler(chunk_frames, hop_frames)
                blocks: asyncio.Queue = asyncio.Queue(maxsize=_BLOCK_QUEUE_MAX)
                self._blocks = blocks
                # #194: track the stream for the except handler. Reset per
                # iteration so a construction failure can't leave a stale
                # reference from the previous pass.
                stream = None
                try:
                    # #164 (CRIT-2 follow-up): resolve the device index INSIDE the
                    # retry loop. A device absent at startup — a mistyped
                    # audio.device_name, or the USB interface not yet enumerated
                    # when the service starts — otherwise made _find_device_index()
                    # raise ABOVE the loop, escaping run() and crash-looping the
                    # process under systemd (Restart=on-failure, 10s). Inside the
                    # loop it degrades to the same backoff-and-rebuild path a stream
                    # construction failure or a CONC-5 stall already take.
                    # #194: re-resolving alone is NOT enough — PortAudio's device
                    # table is frozen at import, so a fresh lookup sees the same
                    # stale snapshot every retry. The except handler below calls
                    # _refresh_audio_devices() to actually re-enumerate before the
                    # next attempt, which is what lets a late-enumerated or
                    # re-plugged device be picked up here.
                    device_index = self._find_device_index()
                    stream = sd.InputStream(
                        samplerate=self.sample_rate,
                        channels=1,
                        dtype="float32",
                        device=device_index,
                        blocksize=int(_BLOCK_SECONDS * self.sample_rate),
                        callback=self._make_callback(loop, blocks),
                    )
                    with stream:
                        while self._running:
                            try:
                                block = await asyncio.wait_for(
                                    blocks.get(), timeout=_BLOCK_STALL_TIMEOUT_SECONDS
                                )
                            except asyncio.TimeoutError:
                                # CONC-5: no block for the stall window while the
                                # stream is open → the PortAudio callback stopped
                                # firing (device brown-out/unplug, or a callback
                                # abort from CFFI). Nothing raised on its own, so
                                # raise here to reuse the tear-down + backoff +
                                # rebuild path below — the same one a stream
                                # CONSTRUCTION failure already takes.
                                raise RuntimeError(
                                    f"audio stream stalled: no block for "
                                    f"{_BLOCK_STALL_TIMEOUT_SECONDS}s (device "
                                    f"brown-out/unplug or callback abort)"
                                )
                            if block is None:
                                continue  # stop() sentinel — re-check self._running
                            for chunk in assembler.feed(block):
                                await self._dispatch_chunk(chunk, self.sample_rate)
                except Exception as e:
                    # CancelledError is BaseException and intentionally NOT caught
                    # here — shutdown cancellation propagates to main() cleanly.
                    self._log_capture_error(e)
                    # #194: if construction succeeded (Pa_OpenStream) but the
                    # `with` __enter__/start() then raised — a device brown-out or
                    # unplug in the window between open and start, exactly the
                    # hotplug class this fixes — Python never runs __exit__, so the
                    # stream is still OPEN. _terminate() (in _refresh_audio_devices)
                    # against a live stream is undefined behaviour, so close it
                    # first. Idempotent: on the normal error paths __exit__ has
                    # already closed it, and close() is safe to call again.
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                    # Then refresh PortAudio's frozen device table before the next
                    # attempt so a device that appeared or moved (late USB
                    # enumeration, or a re-plug to a new ALSA index after a CONC-5
                    # stall) can actually be found on the retry. No stream is live
                    # now — the branch above guaranteed it.
                    self._refresh_audio_devices()
                    await asyncio.sleep(_STREAM_RETRY_BACKOFF_SECONDS)  # Then retry with a fresh stream
        finally:
            # Tear the ticker down with the capture loop (covers normal exit and
            # cancellation), and await it so it doesn't outlive run().
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass

    def stop(self):
        self._running = False
        # Wake run() if it's parked on blocks.get() so it can observe
        # self._running == False without needing to be cancelled.
        if self._blocks is not None:
            try:
                self._blocks.put_nowait(None)
            except asyncio.QueueFull:
                pass  # run() has plenty to wake up for already
        log.info("Audio capture stopped.")
