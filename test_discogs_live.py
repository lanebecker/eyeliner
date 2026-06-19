#!/usr/bin/env python3
"""Live integration check for the Discogs reader/writer.

Hits the real Discogs API using your config.yaml credentials.
All checks are read-only by default.

Usage:
    python test_discogs_live.py                # read-only
    python test_discogs_live.py --test-write   # also tests increment_play_count
                                               # (WRITES to your Discogs collection)
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

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

def check_search_collection(client) -> Optional[dict]:
    sep(f"1 · search_collection  —  {TEST_ARTIST} / {TEST_ALBUM}")
    try:
        result = client.search_collection(TEST_ARTIST, TEST_ALBUM)
    except Exception as e:
        fail(f"Exception: {e}")
        return None

    if result is None:
        fail("Not found in your collection.")
        info(f"Is '{TEST_ALBUM}' by {TEST_ARTIST} in your Discogs?")
        info("If so, try adjusting the artist/album strings at the top of this script.")
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


def check_search_database(client):
    sep(f"2 · search_database  —  {TEST_ARTIST} / {TEST_ALBUM}")
    try:
        result = client.search_database(TEST_ARTIST, TEST_ALBUM)
    except Exception as e:
        fail(f"Exception: {e}")
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
        fail(f"Exception: {e}")
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
        fields = client._get_collection_fields()
    except Exception as e:
        fail(f"Exception: {e}")
        return

    if not fields:
        fail("No custom fields found — have you added any in Discogs?")
        return

    ok(f"{len(fields)} custom field(s) in your collection:")
    for name, fid in fields.items():
        target = name == client.play_count_field_name
        marker = "  ← this is the one we update" if target else ""
        info(f"    [{fid}]  {name}{marker}")

    if client.play_count_field_name not in fields:
        fail(
            f"Field '{client.play_count_field_name}' not found!\n"
            f"     Check that play_count_field_name in config.yaml matches exactly "
            f"(case-sensitive)."
        )


def check_increment_play_count(client, collection_result: Optional[dict]):
    sep("5 · increment_play_count  —  WRITE TEST")
    if collection_result is None:
        fail("Skipping — requires a successful search_collection result first.")
        return

    release_id  = collection_result["release_id"]
    instance_id = collection_result["instance_id"]

    info(f"About to increment '{client.play_count_field_name}'")
    info(f"Release {release_id}, instance {instance_id}")

    try:
        success = client.increment_play_count(release_id, instance_id)
    except Exception as e:
        fail(f"Exception: {e}")
        return

    if success:
        ok("Play Count incremented! Check your Discogs collection to confirm.")
        info("You can reset the value manually in Discogs if needed.")
    else:
        fail("Update failed — check the error logged above.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Live integration test for the vinyl-now-playing Discogs client."
    )
    parser.add_argument(
        "--test-write",
        action="store_true",
        help="Also run increment_play_count — WRITES to your Discogs collection.",
    )
    args = parser.parse_args()

    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║    vinyl-now-playing  ·  Discogs live test           ║")
    print("  ╚══════════════════════════════════════════════════════╝")

    # Make sure src/ imports resolve when running from the project root
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from src.config import load_config, ConfigError
    from src.metadata.discogs import DiscogsHttp, DiscogsReader, DiscogsCollectionWriter

    try:
        config = load_config()
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
    info(f"Test album:       {TEST_ARTIST} / {TEST_ALBUM}")
    if args.test_write:
        info("Mode:             READ + WRITE (--test-write)")
    else:
        info("Mode:             read-only  (pass --test-write to also test the field update)")

    # Run tests
    collection_result = check_search_collection(reader)
    check_search_database(reader)

    if collection_result:
        check_get_tracklist(reader, collection_result["release_id"])

    check_collection_fields(writer)

    if args.test_write:
        check_increment_play_count(writer, collection_result)
    else:
        sep("5 · increment_play_count  —  skipped (read-only mode)")
        info("Run with --test-write to also test the Play Count increment.")

    sep()
    print("  Done.\n")


if __name__ == "__main__":
    main()
