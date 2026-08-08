"""Regression tests for #179 (R4:gap1-1) — strategy-2 collection matching.

The old strategy 2 matched with bare substring containment
(``album_lower in title and any(artist_lower in a for a in artists)``),
iterating the index most-recently-added-first.  Executed reproductions showed
four failure modes, each of which selects a WRONG ``instance_id`` — the Discogs
Play Count / Last Played write target — or silently misses an owned record:

1. Superstring sibling:  "led zeppelin ii" ⊂ "led zeppelin iii", so owning both
   (III added later) wrote II's plays onto III.
2. Cross-artist containment:  "war" ⊂ "warpaint" (artist AND title), so playing
   War's unowned "War" credited an owned Warpaint "Warpaint".
3. Self-titled families:  all four Peter Gabriel albums are titled "Peter
   Gabriel" on Discogs; the walk silently picked the most-recently-added one.
4. One-directional miss:  a decorated Shazam album ("Rumours (Deluxe Edition)")
   missed an exactly-owned "Rumours".

The fix is tiered exact-first matching on normalised strings (punctuation fold
+ NFKC + casefold + whitespace collapse, with the Discogs " (n)" disambiguation
suffix stripped from INDEX ARTIST names): tier 1 requires exact album+artist
equality; tier 2 retries with a trailing parenthetical stripped from ONE album
side at a time (never both — distinct parenthetical siblings are different
albums).  Either tier must produce a UNIQUE match across the whole index — on
ambiguity the matcher refuses to guess and returns None (SEC-1 principle),
degrading the track to the database tier (no instance_id, no write target).
"""
from unittest.mock import MagicMock

from tests.factories import make_discogs_reader


def _reader_with_index(entries):
    """A DiscogsReader whose session index is pre-built from ``entries``
    (insertion order = collection "added desc", newest first) and whose
    strategy 1 finds nothing, so search_collection exercises strategy 2 only.

    The release fetch + result build are faked at the seam below strategy 2's
    decision, so the ``instance_id`` in the result is exactly the one the
    matcher selected.
    """
    reader = make_discogs_reader()
    reader._collection_index = {
        release_id: {"instance_id": instance_id, "title": title, "artists": artists}
        for release_id, instance_id, title, artists in entries
    }
    reader._database_search = MagicMock(return_value=[])   # strategy 1 misses
    reader._client.release = MagicMock(side_effect=lambda rid: MagicMock(id=rid))
    reader._build_result = MagicMock(
        side_effect=lambda release, instance_id: {
            "release_id": release.id, "instance_id": instance_id,
        }
    )
    return reader


# ---------------------------------------------------------------------------
# The four reproduced failure modes (RED against the containment matcher).
# ---------------------------------------------------------------------------

def test_superstring_sibling_credits_the_exactly_owned_record():
    """Owning II and III (III added later): playing II must credit II, even
    though "led zeppelin ii" is a substring of "led zeppelin iii" and III is
    earlier in the added-desc walk."""
    reader = _reader_with_index([
        (5001, 91, "Led Zeppelin III", ["Led Zeppelin"]),   # added later
        (5002, 92, "Led Zeppelin II", ["Led Zeppelin"]),
    ])

    result = reader.search_collection("Led Zeppelin", "Led Zeppelin II")

    assert result is not None
    assert result["instance_id"] == 92          # II, not III's 91


def test_cross_artist_containment_does_not_credit_a_different_owned_record():
    """Playing War's "War" (not owned) must NOT match an owned Warpaint
    "Warpaint" just because "war" is a substring of both fields."""
    reader = _reader_with_index([
        (7001, 55, "Warpaint", ["Warpaint"]),
    ])

    assert reader.search_collection("War", "War") is None


def test_self_titled_family_ambiguity_refuses_to_guess():
    """Two owned self-titled albums that normalise identically: there is no
    principled way to pick a write target, so the matcher must return None
    rather than silently credit the most-recently-added one."""
    reader = _reader_with_index([
        (8002, 72, "Peter Gabriel", ["Peter Gabriel"]),     # added later
        (8001, 71, "Peter Gabriel", ["Peter Gabriel"]),
    ])

    assert reader.search_collection("Peter Gabriel", "Peter Gabriel") is None


def test_decorated_shazam_album_matches_the_plainly_titled_owned_record():
    """The old containment was one-directional: "Rumours (Deluxe Edition)"
    missed an owned "Rumours".  Tier 2 strips the trailing parenthetical."""
    reader = _reader_with_index([
        (9001, 33, "Rumours", ["Fleetwood Mac"]),
    ])

    result = reader.search_collection("Fleetwood Mac", "Rumours (Deluxe Edition)")

    assert result is not None
    assert result["instance_id"] == 33


def test_discogs_artist_disambiguation_suffix_is_stripped_for_matching():
    """Discogs renders same-named artists as "Name (2)" etc.; the suffix must
    not block an exact artist match."""
    reader = _reader_with_index([
        (9101, 44, "Sister", ["Sonic Youth (2)"]),
    ])

    result = reader.search_collection("Sonic Youth", "Sister")

    assert result is not None
    assert result["instance_id"] == 44


def test_disambiguation_suffix_alone_is_not_a_wrong_band_match():
    """A different band of the same name ("Nirvana (2)", UK 60s) with a
    DIFFERENT album must not match — exact title equality protects here where
    the old containment ("nirvana" ⊂ "nirvana (2)") did not."""
    reader = _reader_with_index([
        (9201, 66, "Local Anaesthetic", ["Nirvana (2)"]),
    ])

    assert reader.search_collection("Nirvana", "Nevermind") is None


# ---------------------------------------------------------------------------
# Behaviour that must keep working.
# ---------------------------------------------------------------------------

def test_exact_match_still_works_case_and_whitespace_insensitively():
    reader = _reader_with_index([
        (5003, 93, "  Physical  Graffiti ", ["Led Zeppelin"]),   # doubled interior space
    ])

    result = reader.search_collection("led zeppelin", "physical graffiti")

    assert result is not None
    assert result["instance_id"] == 93


def test_matching_album_title_with_wrong_artist_does_not_match():
    """Title equality alone is not ownership: the artist must also match
    exactly (many albums share a title across artists)."""
    reader = _reader_with_index([
        (5006, 96, "Low", ["David Bowie"]),
    ])

    assert reader.search_collection("Testament", "Low") is None


def test_tier2_never_matches_on_an_empty_stripped_album():
    """A pure-parenthetical album strips to the empty string; that must not
    match an owned entry whose title also strips to empty — nor one whose
    index title is empty outright (a degenerate API payload), which would
    satisfy an unguarded ``"" == ""`` tier-2 comparison."""
    reader = _reader_with_index([
        (5007, 97, "(Blank)", ["Some Artist"]),
    ])
    assert reader.search_collection("Some Artist", "(Nothing)") is None

    reader = _reader_with_index([
        (5009, 99, "", ["Some Artist"]),        # empty title from the API
    ])
    assert reader.search_collection("Some Artist", "(Nothing)") is None


def test_unicode_divergence_is_normalised():
    """Typographic apostrophe (Shazam) vs ASCII (Discogs index) must match:
    normalisation is NFKC + casefold on both sides."""
    reader = _reader_with_index([
        (5004, 94, "Donʼt Stand Me Down", ["Dexys Midnight Runners"]),
    ])

    result = reader.search_collection(
        "Dexys Midnight Runners", "Don’t Stand Me Down"
    )

    assert result is not None
    assert result["instance_id"] == 94


def test_nfkc_outputs_that_are_fold_table_keys_are_also_folded():
    """#179 second-pass regression: some characters' NFKC OUTPUT is itself a
    fold-table key (fullwidth grave U+FF40 → U+0060), so the fold must run
    after NFKC as well as before."""
    reader = _reader_with_index([
        (5010, 90, "Rock 'N' Roll", ["Some Artist"]),
    ])

    result = reader.search_collection("Some Artist", "Rock ｀N｀ Roll")

    assert result is not None
    assert result["instance_id"] == 90


def test_spacing_acute_apostrophes_are_folded_before_nfkc():
    """#179 cold-review regression: NFKC decomposes a spacing acute (´) into
    space + combining accent, so the punctuation fold must run BEFORE NFKC or
    the ``´`` → ``'`` mapping never fires and the titles can't match."""
    reader = _reader_with_index([
        (5008, 98, "Rock ´N´ Roll", ["Some Artist"]),
    ])

    result = reader.search_collection("Some Artist", "Rock 'N' Roll")

    assert result is not None
    assert result["instance_id"] == 98


def test_decorated_index_title_matches_a_plain_shazam_album():
    """Reverse direction of the Rumours case: the OWNED entry carries the
    decoration and the Shazam album is plain.  Unique → must match."""
    reader = _reader_with_index([
        (9303, 83, "Zenyatta Mondatta (Remastered)", ["The Police"]),
    ])

    result = reader.search_collection("The Police", "Zenyatta Mondatta")

    assert result is not None
    assert result["instance_id"] == 83


def test_distinct_trailing_parentheticals_are_different_albums():
    """#179 cold-review regression: two titles that differ ONLY in their
    trailing parenthetical are DIFFERENT albums.  A both-sides strip would
    equate them and credit the wrong record; one-side-at-a-time must not."""
    reader = _reader_with_index([
        (9401, 84, "Live (1980)", ["Thin Lizzy"]),
    ])
    assert reader.search_collection("Thin Lizzy", "Live (1975)") is None

    reader = _reader_with_index([
        (9402, 85, "Greatest Hits (Volume Two)", ["Some Band"]),
    ])
    assert reader.search_collection("Some Band", "Greatest Hits (Volume One)") is None


def test_tier2_ambiguity_also_refuses_to_guess():
    """Two owned decorated variants that both normalise to the searched album
    once parentheticals are stripped: still no unique target — None."""
    reader = _reader_with_index([
        (9301, 81, "The Wall (UK)", ["Pink Floyd"]),
        (9302, 82, "The Wall (US)", ["Pink Floyd"]),
    ])

    assert reader.search_collection("Pink Floyd", "The Wall") is None


def test_strategy2_error_propagates_for_retry_not_swallowed_as_not_owned():
    """B-4/B-13 parity is preserved: a transient fetch error on a matched
    entry propagates (album stays uncached, retried next track) instead of
    downgrading to "not owned"."""
    reader = _reader_with_index([
        (5005, 95, "Sister", ["Sonic Youth"]),
    ])
    reader._client.release = MagicMock(side_effect=ConnectionError("blip"))

    try:
        reader.search_collection("Sonic Youth", "Sister")
    except ConnectionError:
        pass
    else:
        raise AssertionError("expected the transient fetch error to propagate")
