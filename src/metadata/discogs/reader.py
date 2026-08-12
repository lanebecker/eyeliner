"""Read-only Discogs access for the resolver (A-4).

`DiscogsReader` owns everything the metadata resolver needs and nothing it
doesn't: database + collection search, tracklist and original-year lookups, and
assembly of the standardised result dict.  It holds the high-level
python3-discogs-client `Client` (the only half that searches/fetches releases)
and the session-cached collection index; it reaches the REST API through the
shared :class:`~src.metadata.discogs.transport.DiscogsHttp`.

It has no knowledge of the write side (play-count / last-played) — that lives in
:class:`~src.metadata.discogs.writer.DiscogsCollectionWriter`.
"""

import logging
import re
import time
from typing import Optional, TYPE_CHECKING
from urllib.parse import quote

import discogs_client

from src.metadata.models import TracklistEntry
from src.metadata.discogs.transport import DiscogsHttp, _API_BASE, _HTTP_TIMEOUT
from src.metadata.errors import is_transient
from src.metadata.normalize import fold_text, strip_album_decoration

if TYPE_CHECKING:
    from src.config import DiscogsConfig

log = logging.getLogger(__name__)

# STAB-4: absolute ceiling on the collection-index paging loop.  The loop's
# natural exits are an empty page or ``page >= pagination.pages``; a malformed or
# hostile pagination response (a huge ``pages`` value with never-empty pages) or a
# logic bug would otherwise page WITHOUT BOUND.  Under the documented systemd crash
# loop (Restart=on-failure / RestartSec=15, and StartLimitBurst added by this same
# fix) each restart re-pages from zero, so an unbounded build pins the appliance at
# the authenticated 60-request/minute rate limit.  At 100 releases/page this cap is
# 100,000 records — several times the most extreme personal vinyl collection, so it
# never clips a real one; it only bounds the pathological case.
_MAX_COLLECTION_PAGES = 1000

# #191 (stab-2): the appliance runs 24/7 (nothing implements the old "restarts
# daily" premise), so the collection index CANNOT be cached for the whole
# process lifetime — a record added to Discogs mid-uptime would never be seen.
# It gets a monotonic-clock TTL: on access past the TTL the index is rebuilt
# from the API (one paginated re-fetch, ~1 request/100 releases, trivially
# within the 60/min limit). monotonic (not wall-clock) is deliberate: it is
# immune to the Pi's CLOCK_REALTIME jumps, and a reboot — which resets monotonic
# — rebuilds the index anyway, so the reset is harmless. 12h is the safety-net
# bound; the staleness-triggered refresh below credits same-day additions well
# before it elapses.
_COLLECTION_INDEX_TTL_SECONDS = 12 * 3600

# R6-15: the R5-20 one-entry (artist, album) database-search memo exists to
# collapse the 2–3 identical /database/search GETs a SINGLE resolve issues
# (strategy 1, the database tier, the staleness refresh) into one. A whole
# resolve completes in well under this, so a short TTL preserves 100% of that
# intra-resolve dedup — but on a 24/7 appliance that re-plays the SAME record
# hours apart, an untimed memo would replay an hours-old (possibly empty) page,
# pinning a FALLBACK/coverless result past the record's later addition to the
# Discogs DB. Past the TTL the memo is treated as a miss and the query re-fetches.
_DB_SEARCH_MEMO_TTL_SECONDS = 60.0

# #191 (C): a collection miss whose album the Discogs DATABASE does know is the
# signature of a just-added record (or simply one the user does not own). The
# resolver asks the reader to force-refresh the index and re-check ownership;
# this cooldown bounds that speculative re-page, since the same signal fires on
# every genuinely-unowned record too. Seeded to -inf so the first refresh is
# always allowed regardless of the monotonic epoch.
_INDEX_REFRESH_COOLDOWN_SECONDS = 15 * 60

# #179: Discogs disambiguates same-named artists by appending " (2)", " (3)",
# etc. to the NAME field ("Nirvana (2)" is the UK 60s band).  The suffix is a
# Discogs rendering artifact, not part of the artist's name, so it is stripped
# from INDEX artist names before comparison.  Titles never carry it.
_ARTIST_DISAMBIG_RE = re.compile(r"\s*\(\d+\)\s*$")

# #179 tier 2 / #183 / #222: the album-level trailing-decoration strip now lives
# in the shared ``normalize.strip_album_decoration`` (keyword-gated, paren- and
# bracket-aware, bare-year-excluded).  It is applied ONE SIDE AT A TIME (stripped
# query vs raw index title, or raw query vs stripped index title) — never both
# sides in the same comparison, which would equate two DIFFERENT albums that each
# carry a distinct trailing parenthetical ("Live (1975)" vs owned "Live (1980)"),
# re-introducing the wrong-write-target class this exists to kill (#179 cold
# review; regression-pinned).
#
# #222 closed the prior residual: an UNGATED strip credited a plain-titled owned
# family member from a decorated query (Discogs titles every colour-era Weezer
# album "Weezer", so "Weezer (Blue Album)" credited an owned Green "Weezer").
# The keyword gate means "(Blue Album)" and a bare-year "(1975)" are no longer
# treated as decoration, so the decorated query no longer collapses onto the
# plain member; genuine edition decoration ("(Deluxe Edition)", "[30th
# Anniversary]") still strips.  STACKED decorations ("Pet Sounds (Mono)
# (Remastered)") remain a documented conservative miss (one strip only).
#
# R6-14 extends the same keyword-gated strip to the trailing DASH form ("Blinding
# Lights - Single", "... - Live"), so the SAME bounded decorated-query residual
# now applies to it (R6-14 cold-review Finding 1, accepted): a query for a
# decorated PRODUCT the user may not own — the "- Single" 45, a "- Live" album —
# can credit an owned plain-titled same-BASE record when that base is the UNIQUE
# match.  This is the intended reach (own "X", the Shazam string is "X - Single"),
# it fails toward a plausible owned record of the same title (never cross-artist),
# and it is still refused when two owned entries share the base — identical in
# kind and bound to the paren/bracket residual above, only more frequent because
# "- Live"/"- Remastered"/"- Acoustic" are common dash subtitles.


def _reraise_if_transient(exc: BaseException) -> None:
    """#188: a CREDIT-CRITICAL enrichment fetch (the lazy release load and the
    tracklist) failed.  A TRANSIENT failure (429/5xx/network blip) must
    PROPAGATE so the resolve boundary leaves the album uncached/retryable
    (B-4/B-13) — otherwise a degraded result (no cover, empty tracklist → no
    is_last_track → no Play Count) is cached session-long as a Discogs hit and
    the album's Play Count is silently forfeited.  A permanent/malformed
    failure is the caller's to swallow as graceful degradation of that field.

    NOTE the deliberate carve-out (#188 cold review): get_original_year does
    NOT use this — the original release year is DISPLAY-ONLY with a valid
    pressing-year fallback, so it degrades on transient rather than discarding
    an otherwise credit-capable result over a decorative field.
    """
    if is_transient(exc):
        raise exc


def _entry_title_key(entry: dict) -> str:
    """Folded album-title match key for an index entry — precomputed at build
    time (R5-27) and read here, falling back to an on-the-fly fold for a
    hand-built test index that predates the precompute."""
    k = entry.get("_title_key")
    return k if k is not None else _normalize_term(entry["title"])


def _entry_artist_keys(entry: dict) -> list:
    """Folded, disambiguation-stripped per-name artist keys (R5-27 precompute)."""
    k = entry.get("_artist_keys")
    if k is not None:
        return k
    return [_normalize_artist(_ARTIST_DISAMBIG_RE.sub("", a)) for a in entry["artists"]]


def _entry_credit_key(entry: dict) -> str:
    """Folded, disambiguation-stripped full-credit key (R5-27 precompute); the
    fallback mirrors the reader's on-the-fly join for a pre-precompute entry."""
    k = entry.get("_credit_key")
    if k is not None:
        return k
    credit = entry.get("artist_credit")
    if not credit:
        # R7-28: reconstruct for an EMPTY-STRING artist_credit too, not just None.
        # R6-16 tested `is None`, so an entry carrying artist_credit="" (a
        # community-edited blank) returned "" here while its precompute key and the
        # old pre-R6-16 fallback both reconstructed "john and jane" from `artists`
        # — a silent key mismatch. `not credit` restores parity for both empties.
        # R6-16: mirror the precompute / _reconstruct_artist_credit path — strip
        # the Discogs "(n)" disambiguator PER NAME before joining, not once on the
        # joined string. _ARTIST_DISAMBIG_RE is $-anchored, so a single outer strip
        # only catches the LAST name; a mid-string "John (2) and Jane" would keep
        # its "(2)" and no longer mirror the precompute key "john and jane".
        credit = " and ".join(_ARTIST_DISAMBIG_RE.sub("", a) for a in entry["artists"])
    return _normalize_artist(_ARTIST_DISAMBIG_RE.sub("", credit)) if credit else ""


def _reconstruct_artist_credit(raw_artists: list) -> str:
    """Rebuild the full multi-artist credit string Discogs displays, from the
    ``basic_information.artists`` list — each entry's ``name`` plus its ``join``
    connector to the next (`" & "`, `", "`, `" feat. "`, ...).

    R5-07: the collection index stored only the list of individual names, and
    ``entry_artist_matches`` required ONE name to equal the ENTIRE folded query
    artist.  Shazam reports a joint credit as a single joined string ("Robert
    Plant & Alison Krauss"), which equals no single element — so every
    collaboration album owned silently missed the collection on every play
    (metadata degraded to the database tier, no instance_id, no Play Count,
    indistinguishable in the log from an ordinary not-owned miss).  Storing the
    reconstructed credit lets the matcher compare the query against the whole
    credit, exactly (after the shared fold), preserving the exact-or-nothing rule.
    """
    parts: list = []
    for a in raw_artists:
        # Strip the Discogs " (n)" disambiguator per NAME (the match-time strip
        # is $-anchored, so it would only catch it on the LAST artist of a joint
        # credit — a mid-string "John (2) & Jane" would otherwise never match a
        # clean "John & Jane" query, R5-07 cold-review LOW).
        parts.append(_ARTIST_DISAMBIG_RE.sub("", a.get("name", "")))
        j = (a.get("join") or "").strip()
        if not j:
            continue
        parts.append(", " if j == "," else f" {j} ")
    return "".join(parts).strip()


def _normalize_artist(s: str) -> str:
    """Normalise an artist name for collection matching (#223, via #183).

    On top of :func:`_normalize_term`: folds ``&`` to ``and`` and strips one
    leading ``the`` — the two real-world variant classes ("Rolling Stones" vs
    "The Rolling Stones", "Simon & Garfunkel" vs "Simon And Garfunkel") that
    the old ownership-only strategy 1 happened to bridge and exact equality
    silently lost.  ARTIST names only, never titles ("The Wall" must not
    equal "Wall"); anything fuzzier stays out per the exact-or-nothing
    principle.  Symmetric edge: "The The" folds to "the" on both sides.

    The ``&``→``and`` fold now lives in the shared :func:`fold_text` table
    (#225), which runs it on BOTH sides of NFKC — so a fullwidth ``＆`` (U+FF06),
    which only becomes ``&`` via NFKC, is still caught. Only the leading-``the``
    strip is reader/artist-specific and stays here.
    """
    s = fold_text(s)
    if s.startswith("the "):
        s = s[4:]
    return s


def _normalize_term(s: str) -> str:
    """Normalise one side of a collection-match comparison (#179).

    Delegates to the shared :func:`src.metadata.normalize.fold_text` (#225) so
    reader.py and ``SideIndex`` fold titles through ONE table that cannot drift.
    This WIDENS the old reader-local fold by ``&``→``and`` (fold_text folds it;
    the old private ``_PUNCT_FOLD`` did not), so an album titled "Us & Them Live"
    now matches an owned "Us and Them Live" at the album level — the exact
    symmetric, fail-safe fold #180 already applied at the track level.
    """
    return fold_text(s)


class DiscogsReader:
    """Database/collection search, tracklist + original-year, result assembly."""

    def __init__(self, http: DiscogsHttp, config: "DiscogsConfig"):
        self._http = http
        self.username: str = config.username
        # SEC-7: percent-encode the username once for use as a URL PATH SEGMENT
        # (operator-authored, so a '/', '?' or '#' would otherwise reshape the
        # request path). Mirrors DiscogsCollectionWriter; the collection-index
        # GET below uses this encoded form, not the raw ``self.username``.
        self._username_path: str = quote(config.username, safe="")

        # High-level client — used for search() and release() lookups.
        # set_timeout() applies the same timeout discipline to the library's
        # internal fetcher that we apply to our direct session calls; without
        # it, a hung TCP connection in the library can sit on an executor thread
        # indefinitely.
        self._client = discogs_client.Client(
            "vinyl-now-playing/1.0",
            user_token=config.user_token,
        )
        self._client.set_timeout(connect=5, read=_HTTP_TIMEOUT)

        # Lazily-built index of the user's collection:
        #   {release_id: {"instance_id", "title", "artists", "master_id"}}.
        # Building this ONCE and matching locally replaces the per-candidate N+1
        # membership GETs (P-1). It carries a TTL (#191) so a record added during
        # a long uptime is eventually seen rather than pinned to a boot-time
        # snapshot; the staleness refresh below picks up same-day additions.
        self._collection_index: Optional[dict] = None
        # #191: monotonic timestamp of the last successful build, for the TTL.
        # None means "not built via the TTL'd path" (e.g. injected in a test) —
        # such an index is trusted and never expired.
        self._collection_index_built_at: Optional[float] = None
        # #191 (C): monotonic timestamp of the last forced staleness-refresh,
        # for the cooldown. -inf so the first refresh always runs.
        self._last_index_refresh_at: float = float("-inf")
        # R5-20: one-entry memo of the last database search. Within a single
        # resolve the SAME (artist, album) query is issued up to 3 times —
        # strategy 1 (limit 25), the database tier (limit 3), and the staleness
        # refresh's re-run — each an identical /database/search GET. Cache the
        # fetched page keyed by (artist, album) and slice to the requested limit;
        # the next resolve's different query replaces it (no cross-track
        # staleness). R6-15: it also EXPIRES after _DB_SEARCH_MEMO_TTL_SECONDS, so
        # a 24/7 same-record repeat re-fetches instead of replaying a stale page.
        self._db_search_key = None
        self._db_search_page: list = []
        self._db_search_stamp: float = 0.0

    async def run(self, fn, *args):
        """Dispatch one of this reader's blocking methods on the shared,
        dedicated Discogs executor (#61) rather than the default pool.

        Thin delegate to :meth:`DiscogsHttp.run`; the transport owns the one
        pool both halves (reader + writer) share. The resolver calls
        ``await reader.run(reader.search_collection, artist, album)`` in place of
        ``loop.run_in_executor(None, …)``.
        """
        return await self._http.run(fn, *args)

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def search_collection(self, artist: str, album: str) -> Optional[dict]:
        """Search the user's Discogs collection for a release matching artist + album.

        Both strategies match against an in-memory index of the collection
        (built once per TTL window, #191), so neither pays a per-candidate HTTP
        cost (P-1):

        Strategy 1 — database cross-reference (fast):
          Search the Discogs database for up to 25 candidates and check each
          against the local index by release_id.  Returns the first OWNED
          candidate whose index entry exactly matches the recognition on
          normalised title + artist (#183 — ownership alone is not
          acceptance); mismatched owned candidates are skipped and the scan
          continues, deferring fuzzier cases to strategy 2.

        Strategy 2 — index exact-first match (catches rare/obscure pressings):
          If strategy 1 finds nothing, match the index entries on normalised
          artist + album title: tier 1 exact equality, tier 2 a retry with a
          trailing parenthetical stripped from ONE album side at a time.  Either
          tier must identify a UNIQUE owned entry — on ambiguity this refuses
          to guess and returns None (#179; the SEC-1 principle), because the
          winner's instance_id becomes the Play Count / Last Played write
          target and a guessed target corrupts two records' histories at once.

        Returns None if the release is not found in the collection.  The index
        build raises on a hard error so the resolver treats it as
        "couldn't determine" (leaves the album uncached, retries next track)
        rather than a false "not owned" (B-4/B-13).
        """
        # SEC-1: an incomplete recognition (empty / whitespace artist OR album)
        # cannot identify a SPECIFIC owned pressing, so refuse to guess: return
        # None and let the track resolve via the database / fallback tiers (no
        # instance_id, no write).  Historically this guarded the old substring
        # matcher's degenerate empty-term behaviour (#179 replaced it with
        # exact-first tiers); it stays because the principle stands and it
        # short-circuits the index-build cost.  This does NOT reject
        # legitimately short titles ("4", "Q") — only empty/whitespace terms.
        if not artist.strip() or not album.strip():
            log.info(
                "Skipping collection match for an incomplete recognition "
                "(artist=%r, album=%r): cannot identify a specific owned pressing "
                "without both fields, so not selecting a write target.",
                artist, album,
            )
            return None

        index = self._get_collection_index()

        artist_key = _normalize_artist(artist)
        album_key = _normalize_term(album)

        def entry_artist_matches(entry: dict) -> bool:
            # The " (n)" disambiguation suffix is stripped from INDEX names
            # only — Discogs appends it to artist names, never to titles, and
            # Shazam never produces it (#179).  Artist comparison uses the
            # folded form (#223: leading-"the" + "&"/"and") on both sides.
            if artist_key in _entry_artist_keys(entry):
                return True
            # R5-07: a joint credit ("Robert Plant & Alison Krauss") equals no
            # single index name, so also match the reconstructed full credit
            # string, exactly (after the same fold).  Falls back to an " and "
            # join of the names when the entry predates artist_credit (e.g. a
            # hand-built test index) — covering the common "&"/"and" case; a real
            # build stores the exact credit (incl. comma/feat. separators).
            credit_key = _entry_credit_key(entry)
            if credit_key and credit_key == artist_key:
                return True
            # R5-07 residual (deliberately NOT fixed, Lane 2026-08-11): a "Various"
            # compilation is indexed under the single artist "Various", so a track
            # whose Shazam artist is the real performer still misses it. A wildcard
            # here was declined: Shazam almost always reports a track's ORIGINAL
            # album (not the comp title), so the exact-title gate makes it nearly
            # inert — but on a generic-title collision (owning a Various "Greatest
            # Hits") it would OVER-credit a comp for a track not on it, the META-4
            # direction this codebase refuses. Tracked as a documented residual;
            # revisit on real hardware evidence.
            return False

        # #226: does the collection hold TWO DISTINCT albums at this exact
        # normalised (artist, title) — different, both-present master_ids, i.e.
        # the Peter Gabriel self-titled family, NOT mere pressings of one album?
        # If so there is no principled single write target, so strategy 1 must
        # NOT credit whichever member the loose database search happens to
        # surface; it defers to strategy 2, which refuses to guess.  Pressings of
        # ONE album (shared master, or master absent on both) share a master
        # key here, so either pressing stays a valid strategy-1 target — the
        # deliberate multi-pressing behaviour is preserved.
        #
        # The master_id is the ONLY signal that separates "distinct works" from
        # "pressings of one work"; two same-(artist, title) releases with NO
        # master on either are data-indistinguishable, so two DOCUMENTED
        # residuals remain (both accepted; surfaced by the Bundle-11 cold audit):
        #   (1) two DISTINCT master-less same-titled albums still credit one of
        #       them (a wrong write) — but the canonical self-titled families
        #       (Peter Gabriel, Weezer) all carry distinct masters, so this bites
        #       only for obscure master-less releases; preserving the tested
        #       multi-pressing credit (tests 351/385) was chosen over refusing;
        #   (2) two PRESSINGS of one work that Discogs happens to file under
        #       DIFFERENT masters are refused (a missed credit — fail-safe).
        same_key_masters = {
            entry.get("master_id")
            for entry in index.values()
            if _entry_title_key(entry) == album_key
            and entry_artist_matches(entry)
        }
        same_title_is_distinct_albums = len({m for m in same_key_masters if m}) >= 2

        # Strategy 1: database candidates, matched locally against the index.
        # #183: ownership alone is NOT acceptance — the candidate's INDEX entry
        # (clean collection title/artists, no 'Artist - Album' search-title
        # ambiguity) must exactly match the recognition on normalised title +
        # artist.  Discogs' q= relevance ranking freely interleaves
        # similar-titled releases ('Greatest Hits II' for 'Greatest Hits',
        # deluxe/anniversary editions), so first-owned-wins credited a
        # wrong-but-owned album whenever it outranked the right one.  Among
        # exact matches, relevance order still picks the pressing (unchanged).
        # Anything fuzzier is strategy 2's business — the single authority for
        # tiered matching, uniqueness, and refuse-to-guess.
        candidates = self._database_search(artist, album, limit=25)
        for release in candidates:
            entry = index.get(release.id)
            if entry is None:
                continue
            if _entry_title_key(entry) == album_key and entry_artist_matches(entry):
                if same_title_is_distinct_albums:
                    # #226: exact-matching AND owned, but ambiguous across
                    # distinct same-titled albums — defer to strategy 2's
                    # refuse-to-guess rather than credit this candidate.
                    log.info(
                        "Strategy 1: '%s / %s' matches owned release %d, but the "
                        "collection holds ≥2 DISTINCT albums at this title "
                        "(different masters); deferring to strategy 2 to refuse a "
                        "guessed Play Count write target (#226).",
                        artist, album, release.id,
                    )
                    break
                log.debug(
                    f"Found in collection (strategy 1): '{release.title}' "
                    f"(release {release.id}, instance {entry['instance_id']})"
                )
                return self._build_result(release, instance_id=entry["instance_id"])
            log.debug(
                f"Strategy 1: skipping owned candidate '{entry['title']}' "
                f"(release {release.id}) — not an exact match for "
                f"'{artist} / {album}' (#183)."
            )

        # Strategy 2: exact-first match against the index locally (no extra
        # HTTP).  #179 replaced the old bare substring containment, whose
        # executed failure modes all selected a WRONG write target: superstring
        # siblings ("led zeppelin ii" ⊂ "led zeppelin iii"), cross-artist
        # collisions ("war" ⊂ "warpaint" in both fields), silently-picked
        # self-titled family members, and a one-directional miss on decorated
        # Shazam titles ("Rumours (Deluxe Edition)" never matched "Rumours").
        log.debug(
            f"Strategy 1 found nothing for '{artist} / {album}'; "
            f"matching the collection index (exact-first, #179)."
        )

        # Tier 1: exact normalised album + artist equality (keys and the
        # artist rule are shared with strategy 1 above, #183).
        matches = [
            (release_id, entry)
            for release_id, entry in index.items()
            if _entry_title_key(entry) == album_key
            and entry_artist_matches(entry)
        ]

        # Tier 2: retry with a single trailing decoration stripped from ONE side
        # at a time — the decorated-query direction (stripped query vs raw index
        # title: "Rumours (Deluxe Edition)" → owned "Rumours") and the
        # decorated-index direction (raw query vs stripped index title:
        # "The Wall" → owned "The Wall (UK)").  Never stripped-vs-stripped, which
        # would equate two different decorated siblings (see the trailing-strip
        # comment above).  #222: the strip is keyword-gated and bare-year-
        # excluded (``strip_album_decoration``), so "(Blue Album)" / "(1975)" are
        # no longer stripped and a decorated query can't collapse onto a plain
        # owned family member.  Keys are folded, so the strip runs on folded
        # text.  Only reached when tier 1 found nothing.
        if not matches:
            album_key_stripped = strip_album_decoration(album_key)
            query_stripped = album_key_stripped != album_key

            def tier2_album_matches(entry: dict) -> bool:
                # R5-27: use the precomputed folded title key (no per-call re-fold
                # of every index entry on the common miss path, which is where the
                # cost was measured); only the decoration strip runs per entry.
                title_key = _entry_title_key(entry)
                # decorated-query direction: stripped query vs raw index title.
                if query_stripped and album_key_stripped and album_key_stripped == title_key:
                    return True
                # decorated-index direction: raw query vs stripped index title.
                title_key_stripped = strip_album_decoration(title_key)
                return title_key_stripped != title_key and album_key == title_key_stripped

            matches = [
                (release_id, entry)
                for release_id, entry in index.items()
                if tier2_album_matches(entry)
                and entry_artist_matches(entry)
            ]

        if not matches:
            return None
        if len(matches) > 1:
            # Refuse to guess (#179, SEC-1 principle): two owned entries
            # qualify, so there is no principled write target.  Returning None
            # degrades the track to the database tier — correct metadata, no
            # instance_id, no Play Count / Last Played write.
            log.info(
                "Collection match for '%s / %s' is ambiguous (%d owned entries "
                "qualify: %s); refusing to guess a Play Count write target.",
                artist, album, len(matches),
                ", ".join(f"release {rid}" for rid, _ in matches),
            )
            return None

        release_id, entry = matches[0]
        log.debug(
            f"Found in collection (strategy 2): '{entry['title']}' "
            f"(release {release_id}, instance {entry['instance_id']})"
        )
        # Fetch + build like strategy 1.  A fetch/build error is allowed
        # to PROPAGATE rather than be swallowed as "not owned": the index
        # says the user owns this, so a transient blip should leave the
        # album uncached for retry, not downgrade it (B-4/B-13 parity).
        release_obj = self._client.release(release_id)
        return self._build_result(release_obj, instance_id=entry["instance_id"])

    def search_database(self, artist: str, album: str) -> Optional[dict]:
        """Search the full Discogs database (not just the user's collection).

        Returns the best matching release without an instance_id (since we don't
        know if — or which pressing of — it's in the collection).

        Returns None if nothing useful is found.
        """
        candidates = self._database_search(artist, album, limit=3)
        if not candidates:
            return None

        for release in candidates:
            try:
                return self._build_result(release, instance_id=None)
            except Exception as e:
                # #188: a transient failure means the service is down and every
                # candidate would fail the same way — propagate so the album
                # stays uncached/retryable, rather than returning None (a "clean
                # miss" the resolver would cache as a FALLBACK downgrade).
                _reraise_if_transient(e)
                log.debug(f"Failed to build result for release {release.id}: {e}")
                continue

        return None

    def get_tracklist(self, release_id: int) -> list:
        """Fetch and return the full tracklist for a release.

        Filters out Discogs "heading" pseudo-tracks (e.g. "Side A", "Side B")
        which have no position value and aren't playable tracks.
        """
        try:
            release = self._client.release(release_id)
            return self._parse_tracklist(release)
        except Exception as e:
            _reraise_if_transient(e)
            log.warning(f"Failed to fetch tracklist for release {release_id}: {e}")
            return []

    @staticmethod
    def _parse_tracklist(release) -> list:
        """Parse a Discogs Release's ``.tracklist`` into TracklistEntry rows,
        dropping heading pseudo-tracks and positionless entries. Pure — takes an
        ALREADY-FETCHED release object, so `_build_result` can reuse the release
        it already loaded instead of paying a second GET for the same id (R5-19).
        """
        entries = []
        for track in release.tracklist:
            if getattr(track, "type_", None) == "heading":
                continue
            if not track.position:
                continue
            entries.append(TracklistEntry(
                position=track.position,
                title=track.title,
                duration=track.duration or None,
            ))
        return entries

    def get_original_year(self, release) -> Optional[str]:
        """Fetch the ORIGINAL release year from the pressing's master.

        A Discogs release carries the pressing year — a 2026 reissue of a
        2005 album says 2026.  The master carries the original year, which
        is what the display should show (DESIGN.md §7 album schema; product
        decision 2026-06-11).

        One extra GET per album, routed through the rate-limited transport;
        the resolver's album-level cache means it runs once per album per
        session.  Returns None when the release has no master or the lookup
        fails — callers fall back to the pressing year.
        """
        try:
            master = release.master
            master_id = master.id if master else None
        except Exception:
            # #188 cold review: the original-year lookup is DISPLAY-ONLY and has
            # a valid fallback (the pressing year, release.year), UNLIKE the
            # tracklist which gates the Play Count and has none. So a transient
            # blip on the separate master fetch degrades to the pressing year
            # rather than re-raising — re-raising would discard an otherwise
            # complete, credit-capable collection result (instance_id +
            # tracklist) over a decorative field. The credit-critical fetches
            # (release load, get_tracklist) still propagate transient.
            master_id = None
        if not master_id:
            return None

        try:
            resp = self._http.request("GET", f"{_API_BASE}/masters/{master_id}")
            resp.raise_for_status()
            year = resp.json().get("year")
            if year and int(year) > 0:
                return str(year)
        except Exception as e:
            # Display-only field with a pressing-year fallback: degrade rather
            # than abort the credit-capable resolve (#188 cold review).
            log.debug(f"Master year lookup failed for master {master_id}: {e}")
        return None

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _database_search(self, artist: str, album: str, limit: int = 25) -> list:
        """Search the Discogs database and return up to `limit` Release objects.

        A genuine "no matches" returns an empty list; a hard API error (network,
        429, 5xx) is allowed to RAISE so the caller can treat it as "couldn't
        determine" rather than "not found" (B-13).  Swallowing the error here
        used to make every track fall through to the slow collection walk and
        let an owned album be cached as a database/fallback downgrade.
        """
        key = (artist, album)
        now = time.monotonic()
        # R5-20: fetch the page ONCE per distinct (artist, album). page(1) is
        # materialised so the later slice-to-limit is free and no second GET is
        # triggered; the memo is a single entry, replaced on the next query.
        # R6-15: also expire it after the TTL so a same-record repeat hours later
        # re-fetches rather than replaying a stale/empty page.
        if (self._db_search_key != key
                or now - self._db_search_stamp >= _DB_SEARCH_MEMO_TTL_SECONDS):
            results = self._client.search(album, artist=artist, type="release")
            # R7-26: publish the DATA (page + stamp) BEFORE the key, which acts as
            # the "memo is ready" flag the check above reads. A reader that observes
            # the new key is then guaranteed to also see the matching new page —
            # never this query's page returned for a different query's key. The
            # reader path is single-caller today (the resolver serializes reader
            # calls, so this races with nothing), making key-last defence-in-depth
            # on the 2-worker Discogs pool rather than a lock: a reader that catches
            # the stale key mid-fetch merely re-fetches (fail-safe redundant GET),
            # it never mismatches. NOT a general thread-safety guarantee — the
            # check-then-return is only safe because callers are serialized.
            self._db_search_page = list(results.page(1))
            self._db_search_stamp = now
            self._db_search_key = key            # published LAST
        return self._db_search_page[:limit]

    def _get_collection_index(self) -> dict:
        """Build (once per session) and return an in-memory index of the user's
        collection: ``{release_id: {"instance_id", "title", "artists", "master_id"}}``.

        Replaces the old per-candidate membership GET (one per database
        candidate, up to 25) and the full re-walk with a single paginated fetch
        + local lookups (P-1).  #191: the index carries a monotonic TTL
        (_COLLECTION_INDEX_TTL_SECONDS) — the appliance runs 24/7, so a
        process-lifetime cache would never see a record added mid-uptime; on
        access past the TTL the index is rebuilt from the API. Same-day
        additions are picked up sooner by refresh_index_and_research (C).

        #169 (DELIBERATELY DEFERRED — do not re-file):  persisting this index to
        disk across restarts (with a TTL) to skip the re-page was considered and
        declined.  It is an efficiency-only win — the crash-loop it split from
        already shipped in #103 — and a restart re-pages just ~1 request per 100
        releases, well under the 60/min rate limit, roughly once a day.  Against
        that marginal gain, a persisted ``instance_id`` feeds straight into the
        Play Count / Last Played write target, so a stale or corrupt on-disk index
        re-introduces the wrong-write-target class Wave 1 spent 16 issues closing.
        The in-memory rebuild is the safe default; revisit only if restarts /
        config reloads ever become frequent enough that the re-page is a real cost
        (and then only behind a versioned + username-keyed + TTL'd + atomically-
        written cache that treats any miss / parse error / corruption / schema or
        username mismatch as "rebuild from API, never authoritative").

        Raises on a hard fetch error so the caller (search_collection) lets it
        propagate to the resolver, which treats it as "couldn't determine" and
        leaves the album uncached for retry — rather than a false "not owned"
        that pins a downgrade for the session (B-4/B-13).  A successfully built
        (possibly empty) index is cached.
        """
        # #191: serve the cached index only while it is within the TTL. A
        # built_at of None means the index was injected (tests) rather than built
        # here — trust it and never expire it. A real build stamps built_at, so
        # the TTL applies and a stale index rebuilds from the API below.
        if self._collection_index is not None and (
            self._collection_index_built_at is None
            or time.monotonic() - self._collection_index_built_at
            < _COLLECTION_INDEX_TTL_SECONDS
        ):
            return self._collection_index

        index: dict = {}
        page = 1
        while True:
            resp = self._http.request(
                "GET",
                f"{_API_BASE}/users/{self._username_path}/collection/folders/0/releases",
                params={"page": page, "per_page": 100, "sort": "added", "sort_order": "desc"},
            )
            resp.raise_for_status()
            data = resp.json()

            releases = data.get("releases", [])
            if not releases:
                break

            for item in releases:
                basic = item.get("basic_information", {})
                release_id = basic.get("id")
                if release_id is None:
                    continue
                # Keep the first instance seen per release (mirrors the old
                # "use instances[0]" behaviour for users who own duplicates).
                if release_id not in index:
                    index[release_id] = {
                        "instance_id": item.get("instance_id"),
                        "title": basic.get("title", ""),
                        "artists": [a.get("name", "") for a in basic.get("artists", [])],
                        # R5-07: the reconstructed multi-artist credit string, so a
                        # Shazam joint credit ("A & B") can match a collaboration
                        # album whose index names are ["A", "B"].
                        "artist_credit": _reconstruct_artist_credit(basic.get("artists", [])),
                        # R5-27: precompute the folded match keys ONCE at build,
                        # so search_collection's #226 distinct-albums scan and
                        # tier-1 comparisons don't re-fold every index entry on
                        # every call (~8ms/miss at 3k records on x86, ~4-5x on the
                        # Pi). Behaviour-identical to folding on the fly.
                        "_title_key": _normalize_term(basic.get("title", "")),
                        "_artist_keys": [
                            _normalize_artist(_ARTIST_DISAMBIG_RE.sub("", a.get("name", "")))
                            for a in basic.get("artists", [])
                        ],
                        "_credit_key": _normalize_artist(_ARTIST_DISAMBIG_RE.sub(
                            "", _reconstruct_artist_credit(basic.get("artists", [])))),
                        # #226: the master groups a work's pressings.  Two owned
                        # entries at the same (artist, title) with DIFFERENT
                        # masters are distinct albums (Peter Gabriel I/III), not
                        # pressings — strategy 1 defers to refuse-to-guess.  0 /
                        # missing means "no master": treated as absent (pressings
                        # of a master-less release stay a valid single target).
                        "master_id": basic.get("master_id") or None,
                    }

            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", 1):
                break
            if page >= _MAX_COLLECTION_PAGES:
                # STAB-4: absolute safety ceiling.  Reaching it means the
                # pagination response is malformed (no real personal collection
                # has 100,000+ records), so stop with the partial index rather
                # than paging without bound and hammering the rate limit.  The
                # partial index is still cached below, so this build is not
                # re-attempted per track.  A partial index can only cause
                # false-negatives (an album beyond the cap resolves via the
                # database tier with no instance_id) — never a wrong write target.
                log.warning(
                    "Collection paging hit the absolute cap of %d pages "
                    "(%d releases indexed); stopping with a partial index. "
                    "Suspect a malformed Discogs pagination response.",
                    _MAX_COLLECTION_PAGES, len(index),
                )
                break
            page += 1

        self._collection_index = index
        self._collection_index_built_at = time.monotonic()   # #191: stamp for the TTL
        log.debug(f"Built collection index: {len(index)} release(s).")
        return index

    def refresh_index_and_research(self, artist: str, album: str) -> Optional[dict]:
        """#191 (C): force a stale-index rebuild and re-check ownership, once per
        cooldown.

        The resolver calls this when the collection tier missed but the Discogs
        DATABASE knows the album — the signature of a record the owner may have
        just added (the index is a snapshot from up to the TTL ago). It discards
        the cached index and re-runs :meth:`search_collection` against a freshly
        built one, returning the owned release if it is now present, else None.

        The cooldown is load-bearing, not cosmetic: the same "missed collection,
        hit database" signal fires on EVERY record the user genuinely does not
        own (the common database-tier case), so without it every unowned-record
        play would trigger a full collection re-page. Within the cooldown this
        returns None immediately and re-pages nothing. The cooldown is stamped on
        the ATTEMPT (before the re-page), so a transiently-failing refresh does
        not hammer Discogs — search_collection's build error still propagates so
        the resolver leaves the album uncached/retryable (B-4).
        """
        now = time.monotonic()
        if now - self._last_index_refresh_at < _INDEX_REFRESH_COOLDOWN_SECONDS:
            return None
        self._last_index_refresh_at = now
        # R5-18: force a rebuild WITHOUT discarding the current index up front.
        # Invalidating before the re-page meant a transient failure mid-rebuild
        # (a dropped GET) left the reader with NO index at all, forcing a full
        # re-page on every subsequent resolve until the next success — throwing
        # away a snapshot that was fine seconds ago. Save the current snapshot,
        # invalidate to force _get_collection_index to rebuild, and restore it if
        # the rebuild raises (swap-on-success). The error still propagates so the
        # resolver leaves the album uncached/retryable (B-4).
        saved_index = self._collection_index
        saved_built_at = self._collection_index_built_at
        self._collection_index = None
        self._collection_index_built_at = None
        try:
            return self.search_collection(artist, album)
        except Exception:
            # _get_collection_index assigns the new index only after a fully
            # successful re-page, so on a raise self._collection_index is still
            # None here — restore the previously-valid snapshot rather than
            # leaving the reader index-less.
            if self._collection_index is None:
                self._collection_index = saved_index
                self._collection_index_built_at = saved_built_at
            raise

    def _build_result(self, release, instance_id: Optional[int]) -> dict:
        """Build a standardised result dict from a Discogs Release object.

        Per-field defensive extraction is an INTENTIONAL design choice, not a
        swallowed error (A-6): the identity fields the rest of the pipeline gates
        on — `release_id` and `instance_id` — are passed in by the caller and are
        always trustworthy; the enrichment fields below (cover, year, label,
        catalog, genres, tracklist) are best-effort decoration, so a missing or
        malformed one degrades that field to None/[] rather than failing the
        whole resolve.  This is graceful degradation of optional data, distinct
        from the transient-vs-unexpected error taxonomy in errors.py that governs
        the resolve *boundary*.
        """
        # Cover art — prefer primary image, fall back to first available
        cover_url = None
        try:
            images = release.images
            if images:
                primary = next(
                    (img for img in images if img.get("type") == "primary"),
                    images[0],
                )
                cover_url = primary.get("uri")
        except Exception as e:
            _reraise_if_transient(e)

        # Label and catalog number
        label_name = None
        catno = None
        try:
            if release.labels:
                label_name = release.labels[0].name
                raw_catno = release.labels[0].catno
                # Discogs uses the string "none" when there's no catalog number
                catno = raw_catno if raw_catno and raw_catno.lower() != "none" else None
        except Exception as e:
            _reraise_if_transient(e)

        # Year — prefer the album's ORIGINAL year from the master (v1.4.2);
        # release.year is the pressing year, so a reissue would otherwise
        # display its repress date.  Falls back to the pressing year when
        # there's no master or the lookup fails.  (Discogs returns 0 for
        # unknown years.)
        year = self.get_original_year(release)
        if year is None:
            try:
                if release.year and release.year > 0:
                    year = str(release.year)
            except Exception as e:
                _reraise_if_transient(e)

        # Tracklist — read off the ALREADY-FETCHED release (R5-19: get_tracklist
        # would construct a fresh lazy Release and re-GET the same /releases/{id},
        # doubling enrichment spend against the 60/min budget). Transient
        # propagation is preserved: a transient blip parsing the credit-critical
        # tracklist re-raises so the album stays uncached/retryable (B-4).
        try:
            tracklist = self._parse_tracklist(release)
        except Exception as e:
            _reraise_if_transient(e)
            log.warning(f"Failed to parse tracklist for release {release.id}: {e}")
            tracklist = []

        # Genres and styles — styles are more specific so they come first.
        # Both are already present in the release object; no extra API call needed.
        genres: list = []
        try:
            if release.styles:
                genres.extend(release.styles)
        except Exception as e:
            _reraise_if_transient(e)
        try:
            if release.genres:
                genres.extend(release.genres)
        except Exception as e:
            _reraise_if_transient(e)

        return {
            "album": release.title,
            "year": year,
            "label": label_name,
            "catalog_number": catno,
            "release_id": release.id,
            "instance_id": instance_id,
            "cover_art_url": cover_url,
            "tracklist": tracklist,
            "genres": genres,
        }
