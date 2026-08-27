"""Typed configuration boundary (A-2).

The app's configuration used to be an untyped ``dict`` loaded from
``config.yaml`` and threaded whole into every component, each of which reached
into ``config["audio"]["sample_rate"]`` and friends in its constructor.  A
missing or misspelled key surfaced as a raw ``KeyError`` deep inside a
constructor at startup, and required-vs-optional was decided ad hoc in seven
different modules with no single source of truth.

This module is that single source of truth.  ``load_config()`` parses and
validates the YAML **once** into a frozen :class:`AppConfig` tree of typed
section dataclasses (:class:`AudioConfig`, :class:`DiscogsConfig`,
:class:`DisplayConfig`, :class:`LastFmConfig`, :class:`RecognitionConfig`).
Every component then receives its own typed section object and reads plain
attributes — no dict indexing, no ``.get()`` defaults scattered around.

Validation is **aggregating**: a bad config reports *every* problem at once in
one :class:`ConfigError` (missing required keys, wrong types, non-mapping
sections), rather than failing on the first ``KeyError`` and hiding the rest.
Unknown keys are tolerated (e.g. the reserved ``recognition.acrcloud``
sub-section in ``config.example.yaml`` that no implemented backend reads yet;
``recognition.audd.api_token`` IS read now), so the schema can stay ahead of the
code.

Field defaults here are the authoritative copies of what used to be inline
``.get(key, default)`` calls in each constructor; the per-key mapping is
documented in CODE_REVIEW_2026-06-17.md (finding A-2).
"""

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Optional

import difflib
import logging
import math
import yaml

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when ``config.yaml`` is missing, unreadable, or invalid.

    The message is human-facing and may span multiple lines (one bullet per
    problem); ``main.py`` logs it and exits non-zero at startup.
    """


def _config_file_mode(file_obj):
    """Return the mode of an opened regular config file, or ``None``.

    The descriptor is inspected rather than the path so the permission decision
    applies to the exact file that will be parsed.  ``None`` is deliberately an
    unsupported/unknown result: callers warn and continue on platforms where
    POSIX mode semantics are unavailable.
    """
    if os.name != "posix":
        return None
    try:
        file_stat = os.fstat(file_obj.fileno())
        if not stat.S_ISREG(file_stat.st_mode):
            return None
        return stat.S_IMODE(file_stat.st_mode)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


# Sentinel marking a field that has no default — it MUST be present in the
# config, otherwise it's a reported error (distinct from a field whose default
# legitimately is ``None``, e.g. discogs.last_played_field_name).
_REQUIRED = object()

# (section, key) pairs whose value is a credential and must NEVER be echoed into
# an error message or log (SEC-3).  A wrong-typed credential in config.yaml — an
# all-digit token YAML reads as int, a ``1e5``-shaped value, ``yes``/``no``, a
# mis-pasted list — would otherwise be interpolated verbatim into the aggregated
# ConfigError that main.py logs to the systemd journal, where it persists across
# reboots.  For these fields we report the observed type but redact the value.
# NOTE: if a future backend adds validated credential fields (e.g. an
# ``acrcloud``/``audd`` api_token, currently unparsed), add its (section, key)
# here — anything routed through ``_field`` echoes its value unless listed.
_SECRET_FIELDS = frozenset({
    ("discogs", "user_token"),
    ("lastfm", "api_key"),
    ("lastfm", "api_secret"),
    ("lastfm", "session_key"),
})


def _coerce(value, kind):
    """Return ``(ok, coerced_value)`` for *value* against the expected *kind*.

    YAML scalars already arrive as Python ``int`` / ``float`` / ``bool`` / ``str``,
    so this is validation with two deliberate niceties:

      * ``float`` fields accept an ``int`` and widen it (``sample_rate``-style
        integers written where a float is expected, or ``0`` for a threshold).
      * ``bool`` is a subclass of ``int`` in Python, so an ``int`` field
        explicitly rejects ``True``/``False`` — otherwise ``fullscreen: true``
        fat-fingered into an int field would silently read as ``1``.
    """
    if kind is bool:
        return isinstance(value, bool), value
    if kind is int:
        return (isinstance(value, int) and not isinstance(value, bool)), value
    if kind is float:
        if isinstance(value, bool):
            return False, value
        if isinstance(value, (int, float)):
            return True, float(value)
        return False, value
    if kind is str:
        return isinstance(value, str), value
    return True, value  # unknown kind: accept as-is


def _field(data: dict, key: str, kind, default, *, section: str, errors: list):
    """Read one typed field from a section dict, accumulating any problem.

    Semantics mirror the old per-constructor access exactly:
      * absent / ``null`` + no default  → record "required but missing", None
      * absent / ``null`` + a default   → return the default (the old ``.get``)
      * present but wrong type          → record a type error, fall back
      * present and correct             → return the (possibly widened) value
    """
    present = key in data and data[key] is not None
    if not present:
        if default is _REQUIRED:
            errors.append(f"  • {section}.{key}: required, but missing")
            return None
        return default

    ok, coerced = _coerce(data[key], kind)
    if not ok:
        # SEC-3: the observed type is what the operator needs; never echo a
        # secret's raw value (it would land in the logged ConfigError).
        shown = "<redacted>" if (section, key) in _SECRET_FIELDS else f"{data[key]!r}"
        errors.append(
            f"  • {section}.{key}: expected {kind.__name__}, got "
            f"{type(data[key]).__name__} ({shown})"
        )
        return None if default is _REQUIRED else default
    return coerced


def _check(ok: bool, message: str, errors: list) -> None:
    """Record a value-domain error unless *ok* (CRIT-1).

    :func:`_field` validates a field's TYPE; this validates its VALUE, run AFTER
    the fields are extracted so a bad value — a zero sample rate, a negative
    overlap — is reported in the SAME aggregated :class:`ConfigError` as any type
    error, honouring config.py's "single source of truth" / "one friendly startup
    failure" promise. Without it a type-valid but out-of-domain value sailed
    through here and crashed the capture leg deep in a constructor (a
    ``ChunkAssembler`` ``ValueError``), which systemd's ``Restart=on-failure``
    turns into a permanent crash loop with a raw traceback.

    Each caller guards its condition on the field being a usable value first
    (``x is None or x > 0``), because a missing/type error already left ``None``
    and recorded its own message — so a domain check never itself raises on that
    ``None`` (``None > 0`` would be a ``TypeError``).
    """
    if not ok:
        errors.append(message)


def _warn_unknown_keys(section: str, data: dict, known, *, tolerated=frozenset()) -> None:
    """R6-24: warn (do NOT fail) on keys the section doesn't recognise.

    Unknown keys stay *tolerated* — the schema is allowed to run ahead of the
    code (the reserved ``recognition.acrcloud`` / ``audd`` sub-sections), so this
    never adds to ``errors``. But a TYPO in an implemented key
    (``scrobble_enable`` for ``scrobble_enabled``, ``overlap_secondss``,
    ``lastfmm``) previously produced a silent behaviour change with zero evidence
    anywhere — the field fell back to its default and nothing said so. Log one
    WARNING per unrecognised key with a did-you-mean, so the typo is diagnosable
    from the journal. *tolerated* names the deliberately-reserved sub-sections
    that must not warn.
    """
    for key in data:
        if key in known or key in tolerated:
            continue
        suggestion = difflib.get_close_matches(str(key), sorted(known), n=1)
        hint = f" (did you mean '{suggestion[0]}'?)" if suggestion else ""
        log.warning(
            "Unknown key '%s' in [%s] of config.yaml — ignored%s", key, section, hint
        )


# ---------------------------------------------------------------------------
# Section schemas
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AudioConfig:
    """``[audio]`` — capture + silence-detection parameters."""
    device_name: str
    sample_rate: int
    chunk_seconds: int
    silence_threshold_rms: float
    session_end_silence_seconds: int
    overlap_seconds: int = 5

    @classmethod
    def from_dict(cls, data: dict, errors: list) -> "AudioConfig":
        s = "audio"
        _warn_unknown_keys(s, data, set(cls.__dataclass_fields__))
        device_name = _field(data, "device_name", str, _REQUIRED, section=s, errors=errors)
        sample_rate = _field(data, "sample_rate", int, _REQUIRED, section=s, errors=errors)
        chunk_seconds = _field(data, "chunk_seconds", int, _REQUIRED, section=s, errors=errors)
        silence_threshold_rms = _field(data, "silence_threshold_rms", float, _REQUIRED, section=s, errors=errors)
        session_end_silence_seconds = _field(data, "session_end_silence_seconds", int, _REQUIRED, section=s, errors=errors)
        overlap_seconds = _field(data, "overlap_seconds", int, 5, section=s, errors=errors)

        # CRIT-1: value-domain checks. sample_rate/chunk_seconds <= 0 make
        # chunk_frames <= 0; a NEGATIVE overlap makes hop = chunk - overlap >
        # chunk_frames — both are rejected by ChunkAssembler and would otherwise
        # crash the capture leg into a systemd crash loop.
        # R5-12: device_name "" substring-matches EVERY input device and silently
        # binds the first — a wrong-capture footgun. Require a non-empty value.
        _check(device_name is None or device_name.strip() != "",
               f"  • {s}.device_name: must not be empty", errors)
        _check(sample_rate is None or sample_rate > 0,
               f"  • {s}.sample_rate: must be > 0, got {sample_rate!r}", errors)
        _check(chunk_seconds is None or chunk_seconds > 0,
               f"  • {s}.chunk_seconds: must be > 0, got {chunk_seconds!r}", errors)
        # NOTE (deliberate deviation from CRIT-1's `0 <= overlap < chunk`): only
        # `overlap >= 0` is enforced here. `overlap >= chunk` is NOT a config
        # error — it is a benign degradation that AudioCapture handles by
        # disabling overlap (the appliance keeps running), so rejecting it here
        # would crash-loop an otherwise-functional appliance. The finding's claim
        # that overlap >= chunk crashes is incorrect: that path is guarded.
        _check(overlap_seconds is None or overlap_seconds >= 0,
               f"  • {s}.overlap_seconds: must be >= 0, got {overlap_seconds!r}", errors)
        # #168 (CRIT-1 domain-sweep sibling): a 0 or negative end-of-session timer
        # makes SESSION_ENDED fire on the FIRST silence tick after any
        # MUSIC_STOPPED — ending the session (and crediting the Play Count)
        # essentially the instant the music pauses. Reject it here, in the same
        # aggregated block, rather than letting it silently mis-behave at runtime.
        _check(session_end_silence_seconds is None or session_end_silence_seconds > 0,
               f"  • {s}.session_end_silence_seconds: must be > 0, got {session_end_silence_seconds!r}", errors)
        # R5-11: silence_threshold_rms is THE recognition gate. 0 / negative make
        # the "rms < threshold" silence test unreachable, so a session never ends
        # and every idle chunk is POSTed to Shazam (~8,640/day, the #193 class);
        # NaN makes BOTH comparisons false, so the detector never transitions and
        # recognition is dead — silently, with no warning. It's the one audio
        # field CRIT-1/#168 didn't domain-check. Require finite and > 0.
        _check(
            silence_threshold_rms is None
            or (math.isfinite(silence_threshold_rms) and silence_threshold_rms > 0),
            f"  • {s}.silence_threshold_rms: must be a finite number > 0, "
            f"got {silence_threshold_rms!r}", errors,
        )

        return cls(
            device_name=device_name,
            sample_rate=sample_rate,
            chunk_seconds=chunk_seconds,
            silence_threshold_rms=silence_threshold_rms,
            session_end_silence_seconds=session_end_silence_seconds,
            overlap_seconds=overlap_seconds,
        )


# R7-17: the literal credential placeholders shipped in config.example.yaml.
# Discogs is the REQUIRED core the product hangs off — unlike the optional Last.fm
# scrobbler, whose placeholders degrade gracefully to "scrobbling disabled" at
# runtime (R6-25). A partially-filled Discogs config left at these placeholders
# validates CLEAN and then 401s on every collection call, surfacing only via #189
# as a systemd-healthy service that never actually works. Reject them at config
# time so the one aggregated startup ConfigError names them and the unit parks at
# exit 78 (R6-27) instead of a silent runtime failure.
_DISCOGS_PLACEHOLDERS = frozenset({
    "YOUR_DISCOGS_TOKEN_HERE",
    "your_discogs_username",
})


@dataclass(frozen=True)
class DiscogsConfig:
    """``[discogs]`` — collection lookups + play-count field names."""
    user_token: str
    username: str
    play_count_field_name: str
    last_played_field_name: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict, errors: list) -> "DiscogsConfig":
        s = "discogs"
        _warn_unknown_keys(s, data, set(cls.__dataclass_fields__))
        user_token = _field(data, "user_token", str, _REQUIRED, section=s, errors=errors)
        username = _field(data, "username", str, _REQUIRED, section=s, errors=errors)
        play_count_field_name = _field(data, "play_count_field_name", str, _REQUIRED, section=s, errors=errors)
        last_played_field_name = _field(data, "last_played_field_name", str, None, section=s, errors=errors)

        # R5-12: an empty required string passes the type check but fails at
        # runtime exactly when it matters — an empty user_token 401s every
        # request, and an empty play_count_field_name makes the end-of-session
        # credit fail the instant a play completes. Reject blanks here so the one
        # friendly startup ConfigError names them, rather than a silent runtime
        # failure. user_token's VALUE is never echoed (SEC-3); the message names
        # only the field.
        _check(user_token is None or user_token.strip() != "",
               f"  • {s}.user_token: must not be empty", errors)
        _check(username is None or username.strip() != "",
               f"  • {s}.username: must not be empty", errors)
        _check(play_count_field_name is None or play_count_field_name.strip() != "",
               f"  • {s}.play_count_field_name: must not be empty", errors)

        # R7-17: reject the config.example.yaml placeholders (never echo the value —
        # SEC-3; name only the field). Discogs is required, so an unedited placeholder
        # is a hard config error, not a runtime 401.
        _check(user_token is None or user_token not in _DISCOGS_PLACEHOLDERS,
               f"  • {s}.user_token: still the config.example.yaml placeholder — get "
               f"your token at https://www.discogs.com/settings/developers", errors)
        _check(username is None or username not in _DISCOGS_PLACEHOLDERS,
               f"  • {s}.username: still the config.example.yaml placeholder — set it "
               f"to your Discogs username", errors)

        return cls(
            user_token=user_token,
            username=username,
            play_count_field_name=play_count_field_name,
            last_played_field_name=last_played_field_name,
        )


@dataclass(frozen=True)
class DisplayConfig:
    """``[display]`` — screen geometry + theming/motion flags."""
    width: int
    height: int
    fullscreen: bool = True
    dynamic_theming: bool = True
    reduced_motion: bool = False
    cover_art_cache_dir: str = "src/display/assets/cache"

    @classmethod
    def from_dict(cls, data: dict, errors: list) -> "DisplayConfig":
        s = "display"
        _warn_unknown_keys(s, data, set(cls.__dataclass_fields__))
        width = _field(data, "width", int, _REQUIRED, section=s, errors=errors)
        height = _field(data, "height", int, _REQUIRED, section=s, errors=errors)
        fullscreen = _field(data, "fullscreen", bool, True, section=s, errors=errors)
        dynamic_theming = _field(data, "dynamic_theming", bool, True, section=s, errors=errors)
        reduced_motion = _field(data, "reduced_motion", bool, False, section=s, errors=errors)
        cover_art_cache_dir = _field(data, "cover_art_cache_dir", str, "src/display/assets/cache", section=s, errors=errors)

        # CRIT-1: a zero/negative screen dimension can't render (the renderer
        # scales every constant by min(width/1024, height/600) and builds a
        # width×height surface).
        _check(width is None or width > 0, f"  • {s}.width: must be > 0, got {width!r}", errors)
        _check(height is None or height > 0, f"  • {s}.height: must be > 0, got {height!r}", errors)

        return cls(
            width=width,
            height=height,
            fullscreen=fullscreen,
            dynamic_theming=dynamic_theming,
            reduced_motion=reduced_motion,
            cover_art_cache_dir=cover_art_cache_dir,
        )


@dataclass(frozen=True)
class LastFmConfig:
    """``[lastfm]`` — optional scrobbling.  The whole section is optional; when
    absent every field takes its default and scrobbling stays disabled."""
    scrobble_enabled: bool = False
    api_key: str = ""
    api_secret: str = ""
    session_key: str = ""
    love_on_completion: bool = False

    @classmethod
    def from_dict(cls, data: dict, errors: list) -> "LastFmConfig":
        s = "lastfm"
        _warn_unknown_keys(s, data, set(cls.__dataclass_fields__))
        return cls(
            scrobble_enabled=_field(data, "scrobble_enabled", bool, False, section=s, errors=errors),
            api_key=_field(data, "api_key", str, "", section=s, errors=errors),
            api_secret=_field(data, "api_secret", str, "", section=s, errors=errors),
            session_key=_field(data, "session_key", str, "", section=s, errors=errors),
            love_on_completion=_field(data, "love_on_completion", bool, False, section=s, errors=errors),
        )


# CRIT-2: the recognition backends this build actually IMPLEMENTS — the allowed
# values for ``recognition.backend``. "shazamio" and "audd" are built;
# config.example.yaml also advertises "acrcloud" as a future option; selecting an
# unimplemented one used to pass config's type check and then raise ValueError
# from RecognitionLoop.__init__ (constructed outside main()'s try/except) into a
# systemd crash loop. This is the SINGLE SOURCE OF TRUTH: RecognitionConfig
# validates against it here, and recognizer._init_backend constructs against it,
# so the two can never drift. Add a backend by adding its name here AND a
# constructor branch in _init_backend.
IMPLEMENTED_BACKENDS = frozenset({"shazamio", "audd"})

# R7-17 (extended for AudD): the literal token placeholder shipped in
# config.example.yaml. Like the Discogs placeholders, an unedited value passes the
# non-empty gate and then fails every AudD call — reject it at config time so the
# startup ConfigError names it (value-free) instead of a silent runtime miss.
# Only checked when backend == "audd".
_AUDD_TOKEN_PLACEHOLDER = "YOUR_AUDD_TOKEN"


@dataclass(frozen=True)
class RecognitionConfig:
    """``[recognition]`` — backend selection + confirmation/miss thresholds."""
    poll_interval_seconds: int
    backend: str = "shazamio"
    confirmation_required: int = 2
    error_after_misses: int = 6
    # Credential for the "audd" backend, read from the nested
    # ``recognition.audd.api_token`` sub-section (see from_dict). Required only
    # when backend == "audd". A secret — never echoed in a ConfigError.
    audd_api_token: str = ""
    # #454: upper bound on how long recognition idles between tracks when it has
    # no Discogs duration to predict the next boundary from (the safety re-check).
    max_idle_recheck_seconds: float = 240.0

    @classmethod
    def from_dict(cls, data: dict, errors: list) -> "RecognitionConfig":
        s = "recognition"
        # The reserved acrcloud / audd sub-sections in config.example.yaml are
        # tolerated (schema-ahead-of-code); every other unknown key warns (R6-24).
        _warn_unknown_keys(s, data, set(cls.__dataclass_fields__),
                           tolerated={"acrcloud", "audd"})
        poll_interval_seconds = _field(data, "poll_interval_seconds", int, _REQUIRED, section=s, errors=errors)
        backend = _field(data, "backend", str, "shazamio", section=s, errors=errors)
        confirmation_required = _field(data, "confirmation_required", int, 2, section=s, errors=errors)
        error_after_misses = _field(data, "error_after_misses", int, 6, section=s, errors=errors)
        max_idle_recheck_seconds = _field(data, "max_idle_recheck_seconds", float, 240.0, section=s, errors=errors)
        # The "audd" backend's token lives in the nested ``recognition.audd``
        # sub-section (schema advertised in config.example.yaml). Read it directly
        # rather than through _field, so the secret VALUE is never echoed into a
        # ConfigError (which main.py logs to the journal). A missing sub-section,
        # missing key, or non-string value all coerce to "" — the single
        # value-free required-when-audd gate below then reports it.
        _audd = data.get("audd")
        _audd_token = _audd.get("api_token") if isinstance(_audd, dict) else None
        audd_api_token = _audd_token if isinstance(_audd_token, str) else ""

        # CRIT-1: a zero poll interval busy-loops the recognition leg; fewer than
        # one confirmation or one miss-to-error is nonsensical thresholding.
        _check(poll_interval_seconds is None or poll_interval_seconds > 0,
               f"  • {s}.poll_interval_seconds: must be > 0, got {poll_interval_seconds!r}", errors)
        _check(confirmation_required is None or confirmation_required >= 1,
               f"  • {s}.confirmation_required: must be >= 1, got {confirmation_required!r}", errors)
        _check(error_after_misses is None or error_after_misses >= 1,
               f"  • {s}.error_after_misses: must be >= 1, got {error_after_misses!r}", errors)
        _check(max_idle_recheck_seconds is None or max_idle_recheck_seconds > 0,
               f"  • {s}.max_idle_recheck_seconds: must be > 0, got {max_idle_recheck_seconds!r}", errors)
        # CRIT-2: a type-valid but UNIMPLEMENTED backend (acrcloud/audd) would pass
        # here and then crash RecognitionLoop.__init__ outside main()'s try/except.
        # (A type error already fell back to the valid "shazamio" default, so this
        # never double-reports.)
        _check(backend is None or backend in IMPLEMENTED_BACKENDS,
               f"  • {s}.backend: must be one of {sorted(IMPLEMENTED_BACKENDS)}, "
               f"got {backend!r}", errors)
        # The audd backend cannot work without a token; require it here rather than
        # fail opaquely at the first recognize(). Value-free message (secret).
        _check(backend != "audd" or bool(audd_api_token.strip()),
               f"  • {s}.audd.api_token: required when {s}.backend is 'audd' "
               f"(get one at https://audd.io)", errors)
        _check(backend != "audd" or audd_api_token != _AUDD_TOKEN_PLACEHOLDER,
               f"  • {s}.audd.api_token: still the config.example.yaml placeholder "
               f"— get one at https://audd.io", errors)

        return cls(
            poll_interval_seconds=poll_interval_seconds,
            backend=backend,
            confirmation_required=confirmation_required,
            error_after_misses=error_after_misses,
            audd_api_token=audd_api_token,
            max_idle_recheck_seconds=max_idle_recheck_seconds,
        )


@dataclass(frozen=True)
class AppConfig:
    """The whole validated configuration: one typed object per section."""
    audio: AudioConfig
    discogs: DiscogsConfig
    display: DisplayConfig
    recognition: RecognitionConfig
    lastfm: LastFmConfig

    @classmethod
    def from_dict(cls, raw: dict) -> "AppConfig":
        """Validate a raw (YAML-parsed) mapping into a typed AppConfig.

        Pure and file-free, so it's unit-testable.  Every problem across every
        section is collected and reported together in one :class:`ConfigError`.
        """
        if not isinstance(raw, dict):
            raise ConfigError(
                f"the top-level config must be a mapping, got {type(raw).__name__}"
            )

        errors: list = []

        # R6-24: warn on a misspelled top-level SECTION ('lastfmm', 'audioo') —
        # otherwise the section is silently ignored and every field in it takes
        # its default with no evidence anywhere.
        _warn_unknown_keys(
            "(top level)", raw,
            {"audio", "discogs", "display", "recognition", "lastfm"},
        )

        def parse(name: str, parser, *, required: bool):
            """Parse one section, or record a single section-level error.

            A missing *required* section reports just "section is required" (we
            skip per-field validation so the error list isn't drowned in
            redundant "field missing" lines).  A missing *optional* section is
            parsed from ``{}`` so every field takes its default.
            """
            value = raw.get(name)
            if value is None:
                if required:
                    errors.append(f"  • [{name}] section is required, but missing")
                    return None
                value = {}
            elif not isinstance(value, dict):
                errors.append(
                    f"  • [{name}] must be a mapping, got {type(value).__name__}"
                )
                return None
            return parser(value, errors)

        audio = parse("audio", AudioConfig.from_dict, required=True)
        discogs = parse("discogs", DiscogsConfig.from_dict, required=True)
        display = parse("display", DisplayConfig.from_dict, required=True)
        recognition = parse("recognition", RecognitionConfig.from_dict, required=True)
        lastfm = parse("lastfm", LastFmConfig.from_dict, required=False)

        if errors:
            raise ConfigError(
                "Invalid configuration in config.yaml:\n" + "\n".join(errors)
            )

        return cls(
            audio=audio,
            discogs=discogs,
            display=display,
            recognition=recognition,
            lastfm=lastfm,
        )


def load_config(path: str = "config.yaml") -> AppConfig:
    """Read *path*, parse + validate it, and return a typed :class:`AppConfig`.

    Raises :class:`ConfigError` (never a bare ``KeyError`` / ``OSError`` /
    ``UnicodeDecodeError``) for a missing, unreadable, mis-typed, non-UTF-8,
    malformed, or empty file, or any schema violation, so the caller can present
    one friendly startup failure and systemd parks the unit at exit 78 rather than
    crash-looping on a raw traceback.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"{path} not found. Copy config.example.yaml to {path} and fill in "
            "your values."
        )
    try:
        with open(p, encoding="utf-8") as f:
            mode = _config_file_mode(f)
            if mode is None:
                log.warning(
                    "Could not verify POSIX permissions for %s; continuing",
                    path,
                )
            elif mode & 0o077:
                raise ConfigError(
                    f"{path} permissions are too permissive (mode {mode:04o}); "
                    f"run chmod 600 {path}"
                )
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}")
    except UnicodeDecodeError as e:
        # R7-16: a non-UTF-8 config.yaml (a Latin-1 smart-quote pasted from a doc,
        # a wrong file entirely) raises UnicodeDecodeError from the read, NOT a
        # yaml.YAMLError. Surface it as a ConfigError so the caller parks at exit 78
        # instead of crash-looping on a bare traceback.
        raise ConfigError(f"{path} is not valid UTF-8 text: {e}")
    except OSError as e:
        # R7-16: an unreadable / mis-typed config.yaml — a root-owned 600 file after
        # a `sudo` edit (PermissionError — the likeliest first-boot slip now that the
        # setup guide tells operators to `chmod 600` it), a directory at the path
        # (IsADirectoryError), or a TOCTOU delete of the file after the exists()
        # check (FileNotFoundError — a broken symlink instead fails the earlier
        # exists() and hits the "not found" branch above). All are OSError; without
        # this the process exits
        # 1 on a raw traceback and systemd churns through its restart budget into
        # start-limit-hit, never reaching the R6-27 "parked at 78" state the docs
        # promise.
        raise ConfigError(f"{path} could not be read: {e}")

    if raw is None:
        raise ConfigError(f"{path} is empty")

    return AppConfig.from_dict(raw)
