"""Unit tests for the typed configuration boundary (A-2).

Covers AppConfig.from_dict (parse + aggregating validation + type coercion +
defaults) and load_config (file handling), plus the frozen-ness of the section
dataclasses.  No filesystem is touched except the explicit load_config tests,
which use tmp_path.
"""
import textwrap

import pytest

from src.config import (
    AppConfig,
    AudioConfig,
    ConfigError,
    DiscogsConfig,
    DisplayConfig,
    LastFmConfig,
    RecognitionConfig,
    load_config,
)


def _valid_raw() -> dict:
    """A minimal fully-valid raw config (only required keys + the lastfm
    section), used as a base that individual tests mutate."""
    return {
        "audio": {
            "device_name": "USB Audio Codec",
            "sample_rate": 44100,
            "chunk_seconds": 15,
            "silence_threshold_rms": 0.01,
            "session_end_silence_seconds": 45,
        },
        "discogs": {
            "user_token": "tok",
            "username": "me",
            "play_count_field_name": "Play Count",
        },
        "display": {"width": 1024, "height": 600},
        "recognition": {"poll_interval_seconds": 30},
        "lastfm": {"scrobble_enabled": False},
    }


# ---------------------------------------------------------------------------
# Happy path + defaults
# ---------------------------------------------------------------------------

def test_valid_config_parses_into_typed_tree():
    cfg = AppConfig.from_dict(_valid_raw())
    assert isinstance(cfg.audio, AudioConfig)
    assert isinstance(cfg.discogs, DiscogsConfig)
    assert isinstance(cfg.display, DisplayConfig)
    assert isinstance(cfg.recognition, RecognitionConfig)
    assert isinstance(cfg.lastfm, LastFmConfig)
    assert cfg.audio.sample_rate == 44100
    assert cfg.discogs.username == "me"


def test_optional_fields_take_their_defaults():
    cfg = AppConfig.from_dict(_valid_raw())
    assert cfg.audio.overlap_seconds == 5
    assert cfg.discogs.last_played_field_name is None
    assert cfg.display.fullscreen is True
    assert cfg.display.dynamic_theming is True
    assert cfg.display.reduced_motion is False
    assert cfg.display.cover_art_cache_dir == "src/display/assets/cache"
    assert cfg.recognition.backend == "shazamio"
    assert cfg.recognition.confirmation_required == 2
    assert cfg.recognition.error_after_misses == 6


def test_provided_values_override_defaults():
    raw = _valid_raw()
    raw["audio"]["overlap_seconds"] = 3
    raw["display"]["fullscreen"] = False
    raw["discogs"]["last_played_field_name"] = "Last Played"
    cfg = AppConfig.from_dict(raw)
    assert cfg.audio.overlap_seconds == 3
    assert cfg.display.fullscreen is False
    assert cfg.discogs.last_played_field_name == "Last Played"


def test_absent_lastfm_section_defaults_to_disabled():
    raw = _valid_raw()
    del raw["lastfm"]
    cfg = AppConfig.from_dict(raw)
    assert cfg.lastfm == LastFmConfig()
    assert cfg.lastfm.scrobble_enabled is False


def test_unknown_keys_are_tolerated():
    """Extra keys (e.g. the unused recognition.acrcloud/audd blocks) don't
    fail validation — the schema can lead the code."""
    raw = _valid_raw()
    raw["recognition"]["acrcloud"] = {"access_key": "x"}
    raw["audio"]["future_flag"] = True
    cfg = AppConfig.from_dict(raw)  # must not raise
    assert cfg.recognition.poll_interval_seconds == 30


# ---------------------------------------------------------------------------
# Validation + aggregation
# ---------------------------------------------------------------------------

def test_missing_required_field_is_reported_with_path():
    raw = _valid_raw()
    del raw["audio"]["sample_rate"]
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict(raw)
    assert "audio.sample_rate" in str(exc.value)


def test_missing_required_section_reports_section_not_fields():
    raw = _valid_raw()
    del raw["discogs"]
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict(raw)
    msg = str(exc.value)
    assert "[discogs] section is required" in msg
    # The per-field noise is suppressed when the whole section is missing.
    assert "discogs.username" not in msg


def test_all_errors_are_aggregated_into_one_exception():
    raw = _valid_raw()
    del raw["audio"]["sample_rate"]
    del raw["display"]["width"]
    raw["discogs"]["username"] = 123  # wrong type
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict(raw)
    msg = str(exc.value)
    assert "audio.sample_rate" in msg
    assert "display.width" in msg
    assert "discogs.username" in msg


def test_non_mapping_section_is_reported():
    raw = _valid_raw()
    raw["audio"] = "not a mapping"
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict(raw)
    assert "[audio] must be a mapping" in str(exc.value)


def test_non_mapping_root_is_reported():
    with pytest.raises(ConfigError):
        AppConfig.from_dict(["not", "a", "dict"])


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------

def test_bool_is_rejected_for_int_field():
    """bool is an int subclass; an int field must not silently accept True."""
    raw = _valid_raw()
    raw["audio"]["sample_rate"] = True
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict(raw)
    assert "audio.sample_rate" in str(exc.value)


def test_int_is_widened_to_float():
    raw = _valid_raw()
    raw["audio"]["silence_threshold_rms"] = 0   # int where float expected
    cfg = AppConfig.from_dict(raw)
    assert cfg.audio.silence_threshold_rms == 0.0
    assert isinstance(cfg.audio.silence_threshold_rms, float)


def test_wrong_type_string_for_int_is_reported():
    raw = _valid_raw()
    raw["display"]["width"] = "1024"
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict(raw)
    assert "display.width" in str(exc.value)


def test_null_value_is_treated_as_absent():
    """A key present but explicitly null falls back to its default (or errors
    if required) — matching YAML's `key:` empty value."""
    raw = _valid_raw()
    raw["audio"]["overlap_seconds"] = None
    cfg = AppConfig.from_dict(raw)
    assert cfg.audio.overlap_seconds == 5


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

def test_section_configs_are_frozen():
    cfg = AppConfig.from_dict(_valid_raw())
    with pytest.raises(Exception):
        cfg.audio.sample_rate = 22050  # frozen dataclass → FrozenInstanceError


# ---------------------------------------------------------------------------
# load_config (file handling)
# ---------------------------------------------------------------------------

def test_load_config_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(str(tmp_path / "nope.yaml"))
    assert "not found" in str(exc.value)


def test_load_config_empty_file_raises_config_error(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ConfigError) as exc:
        load_config(str(p))
    assert "empty" in str(exc.value)


def test_load_config_reads_and_validates(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        audio:
          device_name: "Dev"
          sample_rate: 44100
          chunk_seconds: 15
          silence_threshold_rms: 0.01
          session_end_silence_seconds: 45
        discogs:
          user_token: "tok"
          username: "me"
          play_count_field_name: "Play Count"
        display:
          width: 800
          height: 480
        recognition:
          poll_interval_seconds: 20
    """))
    cfg = load_config(str(p))
    assert cfg.display.width == 800
    assert cfg.recognition.poll_interval_seconds == 20
    assert cfg.lastfm.scrobble_enabled is False  # absent section → defaults


def test_load_config_invalid_yaml_raises_config_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("audio: [unclosed\n")
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_example_config_is_valid():
    """The shipped config.example.yaml must always parse — it's the template
    users copy."""
    cfg = load_config("config.example.yaml")
    assert cfg.audio.device_name
    assert cfg.recognition.backend == "shazamio"


# ---------------------------------------------------------------------------
# CRIT-1 — value-domain validation. Type-valid but out-of-domain values (a zero
# sample rate, a negative overlap) used to sail through config and crash the
# capture leg deep in a constructor (ChunkAssembler ValueError), which systemd
# turns into a permanent crash loop with a raw traceback. They must now be
# reported at config load as one friendly, aggregated ConfigError instead.
# ---------------------------------------------------------------------------

import pytest as _pytest


@_pytest.mark.parametrize("section,key,bad,needle", [
    ("audio", "sample_rate", 0, "audio.sample_rate"),
    ("audio", "sample_rate", -1, "audio.sample_rate"),
    ("audio", "chunk_seconds", 0, "audio.chunk_seconds"),
    ("audio", "overlap_seconds", -5, "audio.overlap_seconds"),   # hop > chunk → crash
    ("display", "width", 0, "display.width"),
    ("display", "height", 0, "display.height"),
    ("recognition", "poll_interval_seconds", 0, "recognition.poll_interval_seconds"),
    ("recognition", "confirmation_required", 0, "recognition.confirmation_required"),
    ("recognition", "error_after_misses", 0, "recognition.error_after_misses"),
])
def test_out_of_domain_value_is_rejected(section, key, bad, needle):
    """A type-valid but domain-invalid value is a friendly ConfigError, not a
    downstream crash."""
    raw = _valid_raw()
    raw[section][key] = bad
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict(raw)
    assert needle in str(exc.value)


def test_overlap_equal_to_chunk_is_NOT_a_config_error():
    """DELIBERATE deviation from the finding's `overlap < chunk`: overlap >= chunk
    is a BENIGN degradation that AudioCapture handles by disabling overlap (the
    appliance still runs). Rejecting it at config would crash-loop an otherwise
    functional appliance, so config does NOT reject it — only the crash vector
    (negative overlap → hop > chunk) is rejected above."""
    raw = _valid_raw()
    raw["audio"]["overlap_seconds"] = raw["audio"]["chunk_seconds"]   # overlap == chunk
    cfg = AppConfig.from_dict(raw)   # must NOT raise
    assert cfg.audio.overlap_seconds == raw["audio"]["chunk_seconds"]


def test_boundary_values_are_accepted():
    """The inclusive/exclusive boundaries are right: sample_rate/width/height/
    poll must be > 0; confirmation_required/error_after_misses must be >= 1;
    overlap must be >= 0. The smallest valid value of each is accepted."""
    raw = _valid_raw()
    raw["audio"]["overlap_seconds"] = 0            # >= 0 ok
    raw["recognition"]["confirmation_required"] = 1  # >= 1 ok
    raw["recognition"]["error_after_misses"] = 1     # >= 1 ok
    cfg = AppConfig.from_dict(raw)
    assert cfg.audio.overlap_seconds == 0
    assert cfg.recognition.confirmation_required == 1
    assert cfg.recognition.error_after_misses == 1


def test_domain_and_type_errors_aggregate_into_one_error():
    """A domain error and a type error in different fields are reported TOGETHER
    in one ConfigError (the aggregating contract extends to domain checks)."""
    raw = _valid_raw()
    raw["audio"]["sample_rate"] = 0          # domain error
    raw["display"]["width"] = "wide"         # type error
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict(raw)
    msg = str(exc.value)
    assert "audio.sample_rate" in msg and "display.width" in msg


def test_domain_check_does_not_raise_on_an_upstream_type_error():
    """A domain check must never itself raise on the None a type error left
    behind — sample_rate as a string is ONE type error, not a TypeError crash
    from `None > 0`."""
    raw = _valid_raw()
    raw["audio"]["sample_rate"] = "forty-four-thousand"   # type error → None
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict(raw)   # must be ConfigError, not TypeError
    assert "audio.sample_rate" in str(exc.value)
