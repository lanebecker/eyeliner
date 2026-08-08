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


# ---------------------------------------------------------------------------
# #183 (R4:gap1-4) — Strategy 1 must validate the candidate against the
# recognition, not accept the first owned release among 25 loose-search
# candidates.  Exact normalised title+artist only; anything fuzzier defers to
# strategy 2 (the single authority for tiered matching + refuse-to-guess).
# ---------------------------------------------------------------------------

def _reader_with_candidates(entries, candidates):
    """A reader whose index holds ``entries`` and whose strategy-1 database
    search returns ``candidates`` (MagicMock releases with .id/.title) in
    Discogs relevance order."""
    reader = _reader_with_index(entries)
    cands = []
    for rid, title in candidates:
        c = MagicMock()
        c.id = rid
        c.title = title
        cands.append(c)
    reader._database_search = MagicMock(return_value=cands)
    return reader


def test_strategy1_does_not_credit_a_similar_titled_owned_album():
    """#183 headline: user owns 'Greatest Hits II', plays a borrowed
    'Greatest Hits'.  The owned GH II pressing appears in the top-25 — it must
    NOT become the write target; with no owned GH anywhere, the result is
    None (no instance_id, no write)."""
    reader = _reader_with_candidates(
        entries=[(7100, 71, "Greatest Hits II", ["Queen"])],
        candidates=[(7100, "Queen - Greatest Hits II"), (7200, "Queen - Greatest Hits")],
    )

    assert reader.search_collection("Queen", "Greatest Hits") is None


def test_strategy1_skips_wrong_titles_to_reach_the_exactly_owned_one():
    """Both GH II and GH owned, Discogs ranks GH II first: strategy 1 must
    skip past the mismatched title and credit the exact match."""
    reader = _reader_with_candidates(
        entries=[
            (7100, 71, "Greatest Hits II", ["Queen"]),
            (7200, 72, "Greatest Hits", ["Queen"]),
        ],
        candidates=[(7100, "Queen - Greatest Hits II"), (7200, "Queen - Greatest Hits")],
    )

    result = reader.search_collection("Queen", "Greatest Hits")

    assert result is not None
    assert result["instance_id"] == 72


def test_strategy1_rejects_an_owned_candidate_with_the_wrong_artist():
    """Search noise: an owned release whose index artist differs from the
    recognition must not be accepted just because it surfaced as a candidate."""
    reader = _reader_with_candidates(
        entries=[(7300, 73, "Low", ["David Bowie"])],
        candidates=[(7300, "David Bowie - Low")],
    )

    assert reader.search_collection("Testament", "Low") is None


def test_strategy1_mismatch_still_falls_through_to_strategy2_tiers():
    """An owned deluxe pressing that isn't an exact match for the plain Shazam
    album is strategy 2's business — and its tier-2 strip credits it."""
    reader = _reader_with_candidates(
        entries=[(7400, 74, "Rumours (Deluxe Edition)", ["Fleetwood Mac"])],
        candidates=[(7400, "Fleetwood Mac - Rumours (Deluxe Edition)")],
    )

    result = reader.search_collection("Fleetwood Mac", "Rumours")

    assert result is not None
    assert result["instance_id"] == 74


def test_strategy1_keeps_scanning_past_a_mismatch_when_strategy2_cannot_rescue():
    """The mismatch must CONTINUE the candidate scan, not abort it: with two
    owned pressings of the album, strategy 2's uniqueness rule refuses the
    same-title pair, so only strategy 1's continued scan can credit the play.
    An early abort would turn this into a silent None."""
    reader = _reader_with_candidates(
        entries=[
            (7100, 71, "Greatest Hits II", ["Queen"]),
            (7200, 72, "Greatest Hits", ["Queen"]),     # pressing 1
            (7201, 78, "Greatest Hits", ["Queen"]),     # pressing 2
        ],
        candidates=[(7100, "Queen - Greatest Hits II"), (7200, "Queen - Greatest Hits")],
    )

    result = reader.search_collection("Queen", "Greatest Hits")

    assert result is not None
    assert result["instance_id"] == 72


def test_strategy1_exact_match_first_candidate_unchanged():
    """Control: the common case — first owned candidate IS the exact album —
    behaves as before."""
    reader = _reader_with_candidates(
        entries=[(7500, 75, "Sister", ["Sonic Youth"])],
        candidates=[(7500, "Sonic Youth - Sister")],
    )

    result = reader.search_collection("Sonic Youth", "Sister")

    assert result is not None
    assert result["instance_id"] == 75


def test_strategy1_pressing_choice_among_exact_matches_keeps_relevance_order():
    """Two owned pressings of the same album: the first in Discogs relevance
    order wins, as before (#183 changes which titles qualify, not the
    pressing-choice rule)."""
    reader = _reader_with_candidates(
        entries=[
            (7600, 76, "Sister", ["Sonic Youth"]),
            (7601, 77, "Sister", ["Sonic Youth"]),
        ],
        candidates=[(7601, "Sonic Youth - Sister"), (7600, "Sonic Youth - Sister")],
    )

    result = reader.search_collection("Sonic Youth", "Sister")

    assert result is not None
    assert result["instance_id"] == 77


# ---------------------------------------------------------------------------
# #183 rework (cold-review catches): artist-name folding (#223) and bracket
# qualifiers, so exact-first matching doesn't lose credits the old
# ownership-only strategy 1 happened to bridge.
# ---------------------------------------------------------------------------

def test_leading_the_artist_variant_still_credits():
    """#223 via #183: Shazam 'Rolling Stones' vs index 'The Rolling Stones' —
    old S1 bridged this via release id alone; exact matching must fold it,
    not lose every play by the artist."""
    reader = _reader_with_candidates(
        entries=[(8100, 81, "Sticky Fingers", ["The Rolling Stones"])],
        candidates=[(8100, "The Rolling Stones - Sticky Fingers")],
    )

    result = reader.search_collection("Rolling Stones", "Sticky Fingers")

    assert result is not None
    assert result["instance_id"] == 81


def test_ampersand_artist_variant_still_credits_via_strategy2():
    """'Simon & Garfunkel' vs index 'Simon And Garfunkel', strategy-2 path."""
    reader = _reader_with_index([
        (8200, 82, "Bookends", ["Simon And Garfunkel"]),
    ])

    result = reader.search_collection("Simon & Garfunkel", "Bookends")

    assert result is not None
    assert result["instance_id"] == 82


def test_the_the_folds_symmetrically():
    """Edge control: the band 'The The' folds to 'the' on both sides."""
    reader = _reader_with_index([
        (8300, 83, "Soul Mining", ["The The"]),
    ])

    result = reader.search_collection("The The", "Soul Mining")

    assert result is not None
    assert result["instance_id"] == 83


def test_the_folding_never_applies_to_titles():
    """'The Wall' must not equal 'Wall' — folding is artist-only."""
    reader = _reader_with_index([
        (8400, 84, "Wall", ["Pink Floyd"]),
    ])

    assert reader.search_collection("Pink Floyd", "The Wall") is None


def test_bracket_qualifier_query_matches_plain_owned_title():
    """#183 rework: '[Deluxe Edition]' is the iTunes bracket form of the
    decoration tier 2 already strips in paren form."""
    reader = _reader_with_index([
        (8500, 85, "Rumours", ["Fleetwood Mac"]),
    ])

    result = reader.search_collection("Fleetwood Mac", "Rumours [Deluxe Edition]")

    assert result is not None
    assert result["instance_id"] == 85


def test_bracket_qualified_owned_title_matches_plain_query():
    reader = _reader_with_index([
        (8600, 86, "Nevermind [30th Anniversary]", ["Nirvana"]),
    ])

    result = reader.search_collection("Nirvana", "Nevermind")

    assert result is not None
    assert result["instance_id"] == 86


def test_distinct_bracket_and_paren_siblings_are_different_albums():
    """One-side-at-a-time still holds across bracket grammar: 'Live [1975]'
    vs an owned 'Live (1980)' must not be equated."""
    reader = _reader_with_index([
        (8700, 87, "Live (1980)", ["Thin Lizzy"]),
    ])

    assert reader.search_collection("Thin Lizzy", "Live [1975]") is None


def test_stacked_decorations_remain_a_conservative_miss():
    """Documented residual: only ONE trailing qualifier is stripped."""
    reader = _reader_with_index([
        (8800, 88, "Pet Sounds (Mono) (Remastered)", ["The Beach Boys"]),
    ])

    assert reader.search_collection("The Beach Boys", "Pet Sounds") is None


def test_fullwidth_ampersand_is_also_folded():
    """#183 second-pass regression: the fullwidth ＆ (U+FF06) only becomes an
    ASCII & via NFKC, so the &-fold must run on both sides of it."""
    reader = _reader_with_index([
        (8900, 89, "Bookends", ["Simon And Garfunkel"]),
    ])

    result = reader.search_collection("Simon ＆ Garfunkel", "Bookends")

    assert result is not None
    assert result["instance_id"] == 89


def test_distinct_bracket_siblings_are_different_albums():
    """Bracket-vs-bracket parity for the sibling protection: 'Live [1975]'
    vs an owned 'Live [1980]' must not be equated."""
    reader = _reader_with_index([
        (9300, 90, "Live [1980]", ["Thin Lizzy"]),
    ])

    assert reader.search_collection("Thin Lizzy", "Live [1975]") is None
