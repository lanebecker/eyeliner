#!/usr/bin/env python3
"""Live integration test for DiscogsClient.

Hits the real Discogs API using your config.yaml credentials.
All tests are read-only by default.

Usage:
    python test_discogs_live.py                # read-only
    python test_discogs_live.py --test-write   # also tests mark_as_listened
                                               # (WRITES to your Discogs collection)
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

import yaml

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


def load_config() -> dict:
    path = Path("config.yaml")
    if not path.exists():
        print("\n  ✗  config.yaml not found.")
        print("     Copy config.example.yaml to config.yaml and fill in your values.\n")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Individual tests
# ---------------------------------------------------------------------------

def test_search_collection(client) -> Optional[dict]:
    sep(f"1 · search_collection  —  {TEST_ARTIST} / {TEST_ALBUM}")
    try:
        result = client.search_collection(TEST_ARTIST, TEST_ALBUM)
    except Exception as e:
        fail(f"Exception: {e}")
        return None

    if result is None:
        fail(f"Not found in your collection.")
        info(f"Is '{TEST_ALBUM}' by {TEST_ARTIST} in your Discogs?")
        info("If so, try adjusting the artist/album strings at the top of this script.")
        return None

    ok(f"Album:      {result['album']}")
    ok(f"Label:      {result.get('label') or '(none)'}")
    ok(f"Year:       {result.get('year') or '(unknown)'}")
    ok(f"Cat. no.:   {result.get('catalog_number') or '(none)'}")
    ok(f"Release ID: {result['release_id']}")
    ok(f"Instance ID:{result['instance_id']}  ← needed for mark_as_listened")
    ok(f"Cover URL:  {(result.get('cover_art_url') or '(none)')[:72]}")

    tracks = result.get("tracklist", [])
    ok(f"Tracklist:  {len(tracks)} track(s)")
    for t in tracks:
        dur = f"  [{t.duration}]" if t.duration else ""
        info(f"    {t.position:<4} {t.title}{dur}")

    return result


def test_search_database(client):
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


def test_get_tracklist(client, release_id: int):
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


def test_collection_fields(client):
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
        target = name == client.listened_field_name
        marker = "  ← this is the one we update" if target else ""
        info(f"    [{fid}]  {name}{marker}")

    if client.listened_field_name not in fields:
        fail(
            f"Field '{client.listened_field_name}' not found!\n"
            f"     Check that listened_field_name in config.yaml matches exactly "
            f"(case-sensitive)."
        )


def test_mark_as_listened(client, collection_result: Optional[dict]):
    sep("5 · mark_as_listened  —  WRITE TEST")
    if collection_result is None:
        fail("Skipping — requires a successful search_collection result first.")
        return

    release_id  = collection_result["release_id"]
    instance_id = collection_result["instance_id"]

    info(f"About to write '{client.listened_field_value}' → '{client.listened_field_name}'")
    info(f"Release {release_id}, instance {instance_id}")

    try:
        success = client.mark_as_listened(release_id, instance_id)
    except Exception as e:
        fail(f"Exception: {e}")
        return

    if success:
        ok("Field updated! Check your Discogs collection to confirm.")
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
        help="Also run mark_as_listened — WRITES to your Discogs collection.",
    )
    args = parser.parse_args()

    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║    vinyl-now-playing  ·  Discogs live test           ║")
    print("  ╚══════════════════════════════════════════════════════╝")

    config = load_config()

    # Make sure src/ imports resolve when running from the project root
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from src.metadata.discogs_client import DiscogsClient

    client = DiscogsClient(config)

    print()
    info(f"User:          {client.username}")
    info(f"Listened field: '{client.listened_field_name}' → '{client.listened_field_value}'")
    info(f"Test album:    {TEST_ARTIST} / {TEST_ALBUM}")
    if args.test_write:
        info("Mode:          READ + WRITE (--test-write)")
    else:
        info("Mode:          read-only  (pass --test-write to also test the field update)")

    # Run tests
    collection_result = test_search_collection(client)
    test_search_database(client)

    if collection_result:
        test_get_tracklist(client, collection_result["release_id"])

    test_collection_fields(client)

    if args.test_write:
        test_mark_as_listened(client, collection_result)
    else:
        sep("5 · mark_as_listened  —  skipped (read-only mode)")
        info("Run with --test-write to also test the field update.")

    sep()
    print("  Done.\n")


if __name__ == "__main__":
    main()
