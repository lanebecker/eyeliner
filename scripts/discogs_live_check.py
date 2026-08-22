#!/usr/bin/env python3
"""Live integration check for the Discogs reader/writer.

Hits the real Discogs API using your config.yaml credentials.
All checks are read-only by default.

Usage:
    python scripts/discogs_live_check.py                # read-only
    python scripts/discogs_live_check.py --test-write   # also tests increment_play_count
                                               # (WRITES to your Discogs collection)
"""

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Security: the python3-discogs-client library authenticates by putting the
# Discogs user token in the URL QUERY (unlike the app's own header-auth
# transport), so a requests transport error — a Wi-Fi drop mid bring-up, which
# is exactly when this script runs — stringifies with `token=<real token>`
# embedded. Redact it before it reaches the terminal, scrollback, tmux/SSH
# logs, or a screen recording of the setup session (#203 / gap2-2). The token
# grants full write access to the operator's Discogs account.
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"(token=)[^&\s]+")


def _redact(value) -> str:
    """Mask any ``token=<value>`` inside text (e.g. a requests exception message)."""
    return _TOKEN_RE.sub(r"\1<redacted>", str(value))


class _RedactTokenFilter(logging.Filter):
    """Scrub ``token=`` from records the library logs to stderr — reader.get_tracklist
    swallows the transport error and log.warning's ``str(e)`` through logging's
    lastResort handler, a second sink the fail() redaction alone wouldn't cover."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            # Never raise out of a filter (it runs before emit()'s fault
            # isolation): a malformed %-format record would otherwise crash the
            # log call. DROP it (return False) rather than passing it on — it can
            # never render, and letting it reach handleError() would dump the raw
            # record.msg/.args (possibly holding the token) to stderr.
            return False
        scrubbed = _redact(rendered)
        if scrubbed != rendered:
            record.msg = scrubbed
            record.args = ()
        return True


# ---------------------------------------------------------------------------
# Test parameters — change to an album you know is in your Discogs collection
# ---------------------------------------------------------------------------
TEST_ARTIST = "Sonic Youth"
TEST_ALBUM = "Sister"
# ---------------------------------------------------------------------------


def sep(title=""):
    width = 62
    if title:
        print(f"\n{'─' * 3} {title} {'─' * max(1, width - len(title) - 5)}")
    else:
        print(f"\n{'─' * width}")


def ok(msg):   print(f"  ✓  {msg}")
def fail(msg): print(f"  ✗  {msg}")
def info(msg): print(f"     {msg}")


# ---------------------------------------------------------------------------
# Individual tests
# ---------------------------------------------------------------------------

def check_search_collection(
    client, artist: str = TEST_ARTIST, album: str = TEST_ALBUM
) -> Optional[dict]:
    sep(f"1 · search_collection  —  {artist} / {album}")
    try:
        result = client.search_collection(artist, album)
    except Exception as e:
        fail(f"Exception: {_redact(e)}")
        return None

    if result is None:
        fail("Not found in your collection.")
        info(f"Is '{album}' by {artist} in your Discogs?")
        info("If so, try adjusting --artist and --album and running the check again.")
        return None

    ok(f"Album:      {result['album']}")
    ok(f"Label:      {result.get('label') or '(none)'}")
    ok(f"Year:       {result.get('year') or '(unknown)'}")
    ok(f"Cat. no.:   {result.get('catalog_number') or '(none)'}")
    ok(f"Release ID: {result['release_id']}")
    ok(f"Instance ID:{result['instance_id']}  ← needed for increment_play_count")
    ok(f"Cover URL:  {(result.get('cover_art_url') or '(none)')[:72]}")

    tracks = result.get("tracklist", [])
    ok(f"Tracklist:  {len(tracks)} track(s)")
    for t in tracks:
        dur = f"  [{t.duration}]" if t.duration else ""
        info(f"    {t.position:<4} {t.title}{dur}")

    return result


def check_search_database(
    client, artist: str = TEST_ARTIST, album: str = TEST_ALBUM
):
    sep(f"2 · search_database  —  {artist} / {album}")
    try:
        result = client.search_database(artist, album)
    except Exception as e:
        fail(f"Exception: {_redact(e)}")
        return

    if result is None:
        fail("Not found in the Discogs database at all — unexpected for a major release.")
        return

    ok(f"Album:      {result['album']}")
    ok(f"Release ID: {result['release_id']}")
    ok(f"Instance ID:{result['instance_id']}  ← should be None (not collection-specific)")
    ok(f"Year:       {result.get('year') or '(unknown)'}")


def check_get_tracklist(client, release_id: int):
    sep(f"3 · get_tracklist  —  release {release_id}")
    try:
        tracks = client.get_tracklist(release_id)
    except Exception as e:
        fail(f"Exception: {_redact(e)}")
        return

    if not tracks:
        fail("No tracks returned.")
        return

    ok(f"{len(tracks)} track(s):")
    for t in tracks:
        dur = f"  [{t.duration}]" if t.duration else ""
        info(f"    {t.position:<4} {t.title}{dur}")


def check_collection_fields(client):
    sep("4 · collection fields")
    try:
        fields = client.get_collection_fields()
    except Exception as e:
        fail(f"Exception: {_redact(e)}")
        return

    if not fields:
        fail("No custom fields found — have you added any in Discogs?")
        return

    # Both of the writer's production targets are marked DISTINCTLY (#199): a
    # single "← this is the one we update" was ambiguous once Last Played is also
    # checked below.
    last_played = getattr(client, "last_played_field_name", None)
    ok(f"{len(fields)} custom field(s) in your collection:")
    for name, fid in fields.items():
        markers = []
        if name == client.play_count_field_name:
            markers.append("← Play Count target")
        if last_played and name == last_played:
            markers.append("← Last Played target")
        marker = ("  " + ", ".join(markers)) if markers else ""
        info(f"    [{fid}]  {name}{marker}")

    if client.play_count_field_name not in fields:
        fail(
            f"Field '{client.play_count_field_name}' not found!\n"
            f"     Check that play_count_field_name in config.yaml matches exactly "
            f"(case-sensitive)."
        )

    # #199 (gap2-1): the writer's OTHER production write — update_last_played — was
    # never verified here, so a case-slip in last_played_field_name ("Last played"
    # vs "Last Played") passed every check green and then silently failed on every
    # session end (update_last_played logs "field not found" and returns False).
    # Mirror the play-count check when the field is configured; when it's
    # intentionally unset, say so (an info line, not a failure — Last Played is
    # optional).
    if last_played:
        if last_played not in fields:
            fail(
                f"Field '{last_played}' not found!\n"
                f"     Check that last_played_field_name in config.yaml matches "
                f"exactly (case-sensitive)."
            )
    else:
        info("last_played_field_name is unset — Last Played won't be recorded "
             "(optional; set it in config.yaml if you have that custom field).")


def check_increment_play_count(
    client,
    collection_result: Optional[dict],
    artist: str = TEST_ARTIST,
    album: str = TEST_ALBUM,
    *,
    confirmed: bool = False,
    input_fn=None,
) -> bool:
    sep("5 · increment_play_count  —  WRITE TEST")
    if collection_result is None:
        fail("Skipping — requires a successful search_collection result first.")
        return False

    release_id  = collection_result["release_id"]
    instance_id = collection_result["instance_id"]

    field_name = client.play_count_field_name
    if not confirmed:
        if input_fn is None:
            input_fn = input
        prompt = (
            "Authorize Discogs WRITE? Type exact lowercase 'yes' to continue, "
            f"or anything else to decline. Artist: {artist}; Album: {album}; "
            f"Field: {field_name}; Release ID: {release_id}; Instance ID: {instance_id}: "
        )
        try:
            response = input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            response = ""
        if response != "yes":
            fail("Write declined; increment_play_count was not called.")
            return False

    # Keep this as the sole writer call, immediately after explicit authorization.
    try:
        success = client.increment_play_count(release_id, instance_id)
    except Exception as e:
        fail(f"Exception: {_redact(e)}")
        return False

    if success:
        ok("Play Count incremented! Check your Discogs collection to confirm.")
        info("You can reset the value manually in Discogs if needed.")
    else:
        fail("Update failed — check the error logged above.")
    return bool(success)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        description="Live integration test for the vinyl-now-playing Discogs client."
    )
    parser.add_argument(
        "--artist",
        help=f"Artist to inspect (read-only default: {TEST_ARTIST!r}).",
    )
    parser.add_argument(
        "--album",
        help=f"Album to inspect (read-only default: {TEST_ALBUM!r}).",
    )
    parser.add_argument(
        "--test-write",
        action="store_true",
        help="Also run increment_play_count — WRITES to your Discogs collection.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="With --test-write and explicit --artist/--album, bypass confirmation.",
    )
    return parser


def main(argv=None, input_fn=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.yes and not args.test_write:
        parser.error("--yes requires --test-write")
    if args.test_write and (args.artist is None or args.album is None):
        parser.error("--test-write requires explicit --artist and --album")

    artist = args.artist if args.artist is not None else TEST_ARTIST
    album = args.album if args.album is not None else TEST_ALBUM

    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║    vinyl-now-playing  ·  Discogs live test           ║")
    print("  ╚══════════════════════════════════════════════════════╝")

    # Make sure src/ imports resolve. This script lives in scripts/, so the
    # project root (which holds src/) is its parent's parent.
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from src.config import load_config, ConfigError
    from src.metadata.discogs import DiscogsHttp, DiscogsReader, DiscogsCollectionWriter

    # #203: route the library's own stderr warnings (reader.get_tracklist swallows
    # a transport error and log.warning's str(e)) through the token-redacting
    # filter — otherwise that second sink leaks the token even when every fail()
    # call is redacted. Attached to the handler basicConfig installs.
    logging.basicConfig(level=logging.WARNING)
    for _handler in logging.getLogger().handlers:
        _handler.addFilter(_RedactTokenFilter())

    try:
        # #204: resolve config.yaml from the repo ROOT (reuse `root`), not the CWD.
        # The script fixes sys.path from root but previously called load_config()
        # with its CWD-relative default 'config.yaml', so running it from anywhere
        # but the repo root (e.g. `python vinyl-now-playing/scripts/...` from $HOME
        # over SSH) failed with a misleading "config.yaml not found. Copy
        # config.example.yaml ..." for an operator whose config.yaml already exists.
        # main.py runs with WorkingDirectory=repo root, so this cannot diverge.
        config = load_config(str(root / "config.yaml"))
    except ConfigError as e:
        print(f"\n  ✗  {e}\n")
        sys.exit(1)

    # A-4: one shared transport; read tests use the reader, write tests the writer.
    http = DiscogsHttp(config.discogs.user_token)
    reader = DiscogsReader(http, config.discogs)
    writer = DiscogsCollectionWriter(http, config.discogs)

    print()
    info(f"User:             {reader.username}")
    info(f"Play Count field: '{writer.play_count_field_name}'")
    info(f"Test album:       {artist} / {album}")
    if args.test_write:
        if args.yes:
            info("Mode:             READ + WRITE (--test-write --yes; confirmation bypassed)")
        else:
            info("Mode:             READ + WRITE (--test-write; exact 'yes' required)")
    else:
        info("Mode:             read-only  (pass --test-write to also test the field update)")

    # Run tests
    collection_result = check_search_collection(reader, artist, album)
    check_search_database(reader, artist, album)

    if collection_result:
        check_get_tracklist(reader, collection_result["release_id"])

    check_collection_fields(writer)

    if args.test_write:
        if not check_increment_play_count(
            writer,
            collection_result,
            artist,
            album,
            confirmed=args.yes,
            input_fn=input_fn,
        ):
            return 1
    else:
        sep("5 · increment_play_count  —  skipped (read-only mode)")
        info("Run with --test-write to also test the Play Count increment.")

    sep()
    print("  Done.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
