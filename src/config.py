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
Unknown keys are tolerated (e.g. the ``recognition.acrcloud`` / ``audd``
sub-sections in ``config.example.yaml`` that no implemented backend reads yet),
so the schema can stay ahead of the code.

Field defaults here are the authoritative copies of what used to be inline
``.get(key, default)`` calls in each constructor; the per-key mapping is
documented in CODE_REVIEW_2026-06-17.md (finding A-2).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


class ConfigError(Exception):
    """Raised when ``config.yaml`` is missing, unreadable, or invalid.

    The message is human-facing and may span multiple lines (one bullet per
    problem); ``main.py`` logs it and exits non-zero at startup.
    """


# Sentinel marking a field that has no default — it MUST be present in the
# config, otherwise it's a reported error (distinct from a field whose default
# legitimately is ``None``, e.g. discogs.last_played_field_name).
_REQUIRED = object()


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
        errors.append(
            f"  • {section}.{key}: expected {kind.__name__}, got "
            f"{type(data[key]).__name__} ({data[key]!r})"
        )
        return None if default is _REQUIRED else default
    return coerced


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
        return cls(
            device_name=_field(data, "device_name", str, _REQUIRED, section=s, errors=errors),
            sample_rate=_field(data, "sample_rate", int, _REQUIRED, section=s, errors=errors),
            chunk_seconds=_field(data, "chunk_seconds", int, _REQUIRED, section=s, errors=errors),
            silence_threshold_rms=_field(data, "silence_threshold_rms", float, _REQUIRED, section=s, errors=errors),
            session_end_silence_seconds=_field(data, "session_end_silence_seconds", int, _REQUIRED, section=s, errors=errors),
            overlap_seconds=_field(data, "overlap_seconds", int, 5, section=s, errors=errors),
        )


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
        return cls(
            user_token=_field(data, "user_token", str, _REQUIRED, section=s, errors=errors),
            username=_field(data, "username", str, _REQUIRED, section=s, errors=errors),
            play_count_field_name=_field(data, "play_count_field_name", str, _REQUIRED, section=s, errors=errors),
            last_played_field_name=_field(data, "last_played_field_name", str, None, section=s, errors=errors),
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
        return cls(
            width=_field(data, "width", int, _REQUIRED, section=s, errors=errors),
            height=_field(data, "height", int, _REQUIRED, section=s, errors=errors),
            fullscreen=_field(data, "fullscreen", bool, True, section=s, errors=errors),
            dynamic_theming=_field(data, "dynamic_theming", bool, True, section=s, errors=errors),
            reduced_motion=_field(data, "reduced_motion", bool, False, section=s, errors=errors),
            cover_art_cache_dir=_field(data, "cover_art_cache_dir", str, "src/display/assets/cache", section=s, errors=errors),
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
        return cls(
            scrobble_enabled=_field(data, "scrobble_enabled", bool, False, section=s, errors=errors),
            api_key=_field(data, "api_key", str, "", section=s, errors=errors),
            api_secret=_field(data, "api_secret", str, "", section=s, errors=errors),
            session_key=_field(data, "session_key", str, "", section=s, errors=errors),
            love_on_completion=_field(data, "love_on_completion", bool, False, section=s, errors=errors),
        )


@dataclass(frozen=True)
class RecognitionConfig:
    """``[recognition]`` — backend selection + confirmation/miss thresholds."""
    poll_interval_seconds: int
    backend: str = "shazamio"
    confirmation_required: int = 2
    error_after_misses: int = 6

    @classmethod
    def from_dict(cls, data: dict, errors: list) -> "RecognitionConfig":
        s = "recognition"
        return cls(
            poll_interval_seconds=_field(data, "poll_interval_seconds", int, _REQUIRED, section=s, errors=errors),
            backend=_field(data, "backend", str, "shazamio", section=s, errors=errors),
            confirmation_required=_field(data, "confirmation_required", int, 2, section=s, errors=errors),
            error_after_misses=_field(data, "error_after_misses", int, 6, section=s, errors=errors),
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

    Raises :class:`ConfigError` (never a bare ``KeyError`` / ``OSError``) for a
    missing file, malformed YAML, an empty file, or any schema violation, so the
    caller can present one friendly startup failure.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"{path} not found. Copy config.example.yaml to {path} and fill in "
            "your values."
        )
    try:
        with open(p) as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}")

    if raw is None:
        raise ConfigError(f"{path} is empty")

    return AppConfig.from_dict(raw)
