"""Tests for scripts/discogs_live_check.py — Wave 4 bundle 3 (#203, #204).

The script isn't a package module, so it's loaded from its path. Covers:
  - #203 (gap2-2): the Discogs token (carried in the URL query by the library) is
    redacted from both the fail() print sink and the stderr logging sink.
  - #204 (gap2-3): config.yaml is resolved from the repo root, not the CWD.
"""
import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import ConfigError

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "discogs_live_check.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("discogs_live_check_mod", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dlc = _load_script()


# ---------------------------------------------------------------------------
# #203 — token redaction
# ---------------------------------------------------------------------------

def test_redact_masks_token_query():
    leaked = ("HTTPSConnectionPool(host='api.discogs.com', port=443): Max retries "
              "exceeded with url: /database/search?q=a&token=REAL_TOKEN_ABC123 (Caused by ...)")
    out = dlc._redact(leaked)
    assert "REAL_TOKEN_ABC123" not in out
    assert "token=<redacted>" in out


def test_redact_leaves_tokenless_text_unchanged():
    s = "Connection refused to api.discogs.com"
    assert dlc._redact(s) == s


def test_fail_exception_path_redacts_token(capsys):
    """The check functions' `fail(f"Exception: {_redact(e)}")` must not print the
    raw token when a library transport error carries it in the URL."""
    client = MagicMock()
    client.search_collection.side_effect = Exception(
        "Max retries exceeded with url: /database/search?q=x&token=REAL_TOKEN_XYZ789"
    )
    result = dlc.check_search_collection(client)
    out = capsys.readouterr().out
    assert result is None
    assert "REAL_TOKEN_XYZ789" not in out
    assert "token=<redacted>" in out


def test_log_filter_drops_malformed_record_without_raising():
    """The stderr redaction filter must not turn a malformed %-format log record
    into an exception that escapes the log call; it drops it (return False) rather
    than letting handleError() dump raw args."""
    filt = dlc._RedactTokenFilter()
    rec = logging.LogRecord("t", logging.WARNING, __file__, 1, "n=%d", ("x",), None)
    assert filt.filter(rec) is False


def test_collection_fields_flags_last_played_case_mismatch(capsys):
    """#199: a case-slip in last_played_field_name must be caught, not sail through
    green (it was never checked before — only play_count was)."""
    client = MagicMock()
    client.get_collection_fields.return_value = {"Play Count": 1, "Last Played": 2}
    client.play_count_field_name = "Play Count"
    client.last_played_field_name = "Last played"   # case slip vs "Last Played"
    dlc.check_collection_fields(client)
    out = capsys.readouterr().out
    assert "Last played" in out and "not found" in out          # flagged
    assert "last_played_field_name in config.yaml" in out        # actionable hint


def test_collection_fields_accepts_matching_last_played(capsys):
    """A correctly-configured Last Played field is marked and not flagged."""
    client = MagicMock()
    client.get_collection_fields.return_value = {"Play Count": 1, "Last Played": 2}
    client.play_count_field_name = "Play Count"
    client.last_played_field_name = "Last Played"
    dlc.check_collection_fields(client)
    out = capsys.readouterr().out
    assert "← Last Played target" in out
    assert "← Play Count target" in out
    assert "not found" not in out


def test_collection_fields_notes_unset_last_played(capsys):
    """An intentionally-unset last_played_field_name is an info line, not a failure."""
    client = MagicMock()
    client.get_collection_fields.return_value = {"Play Count": 1}
    client.play_count_field_name = "Play Count"
    client.last_played_field_name = None
    dlc.check_collection_fields(client)
    out = capsys.readouterr().out
    assert "unset" in out
    assert "not found" not in out


def test_log_filter_scrubs_token_from_records():
    """The stderr sink (reader.get_tracklist's swallowed log.warning) is covered by
    the _RedactTokenFilter, not the fail() redaction."""
    filt = dlc._RedactTokenFilter()
    rec = logging.LogRecord(
        "t", logging.WARNING, __file__, 1,
        "Failed to fetch tracklist: /database/...?token=SECRET_TOKEN_9 (err)", (), None,
    )
    assert filt.filter(rec) is True
    out = rec.getMessage()
    assert "SECRET_TOKEN_9" not in out
    assert "token=<redacted>" in out


# ---------------------------------------------------------------------------
# #204 — config resolved from repo root, not CWD
# ---------------------------------------------------------------------------

def test_main_loads_config_from_repo_root_not_cwd(monkeypatch, tmp_path):
    """Run from an unrelated CWD, main() must still resolve config.yaml at the repo
    root (script parent.parent), not the CWD-relative default."""
    captured = {}

    def fake_load_config(path=None):
        captured["path"] = path
        raise ConfigError("stop after path capture")

    # main() does `from src.config import load_config` at call time, so patching
    # the attribute on src.config is what it picks up.
    monkeypatch.setattr("src.config.load_config", fake_load_config)
    monkeypatch.setattr(sys, "argv", ["discogs_live_check.py"])
    monkeypatch.chdir(tmp_path)  # CWD deliberately != repo root

    with pytest.raises(SystemExit) as ei:
        dlc.main()
    assert ei.value.code == 1

    expected = str(Path(dlc.__file__).resolve().parent.parent / "config.yaml")
    assert captured["path"] == expected


# ---------------------------------------------------------------------------
# #366 — explicit record selection and write authorization
# ---------------------------------------------------------------------------

def test_search_operations_use_selected_artist_and_album(capsys):
    client = MagicMock()
    client.search_collection.return_value = None
    client.search_database.return_value = None

    dlc.check_search_collection(client, "Selected Artist", "Selected Album")
    dlc.check_search_database(client, "Selected Artist", "Selected Album")

    client.search_collection.assert_called_once_with("Selected Artist", "Selected Album")
    client.search_database.assert_called_once_with("Selected Artist", "Selected Album")
    out = capsys.readouterr().out
    assert "Selected Artist / Selected Album" in out


@pytest.mark.parametrize("response", ["n", "", " yes", "yes ", "YES", "Yes"])
def test_write_requires_exact_yes(response, capsys):
    client = MagicMock()
    result = {"release_id": 123, "instance_id": 456}

    accepted = dlc.check_increment_play_count(
        client,
        result,
        "Selected Artist",
        "Selected Album",
        input_fn=lambda _prompt: response,
    )

    assert accepted is False
    client.increment_play_count.assert_not_called()
    assert "Write declined" in capsys.readouterr().out


def test_write_declines_on_eof(capsys):
    client = MagicMock()
    result = {"release_id": 123, "instance_id": 456}

    def eof(_prompt):
        raise EOFError

    accepted = dlc.check_increment_play_count(
        client,
        result,
        "Selected Artist",
        "Selected Album",
        input_fn=eof,
    )

    assert accepted is False
    client.increment_play_count.assert_not_called()
    assert "Write declined" in capsys.readouterr().out


def test_write_prompt_contains_selection_and_ids_but_not_token(capsys):
    client = MagicMock()
    client.play_count_field_name = "Play Count"
    client.increment_play_count.return_value = True
    result = {"release_id": 123, "instance_id": 456}
    prompts = []

    def authorize(prompt):
        prompts.append(prompt)
        return "yes"

    assert dlc.check_increment_play_count(
        client,
        result,
        "Selected Artist",
        "Selected Album",
        input_fn=authorize,
    ) is True
    client.increment_play_count.assert_called_once_with(123, 456)
    assert len(prompts) == 1
    prompt = prompts[0]
    assert "Selected Artist" in prompt
    assert "Selected Album" in prompt
    assert "Play Count" in prompt
    assert "123" in prompt and "456" in prompt
    assert "token" not in prompt.lower()
    capsys.readouterr()


def test_yes_bypasses_prompt_only_for_explicit_write_selection(monkeypatch):
    with pytest.raises(SystemExit) as ei:
        dlc.main(["--yes"])
    assert ei.value.code == 2

    with pytest.raises(SystemExit) as ei:
        dlc.main(["--test-write", "--artist", "Artist"])
    assert ei.value.code == 2

    parser = dlc._build_parser()
    args = parser.parse_args([
        "--test-write", "--yes", "--artist", "Artist", "--album", "Album"
    ])
    assert args.yes is True
    assert args.artist == "Artist"
    assert args.album == "Album"
