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
import unicodedata
from typing import Optional, TYPE_CHECKING
from urllib.parse import quote

import discogs_client

from src.metadata.models import TracklistEntry
from src.metadata.discogs.transport import DiscogsHttp, _API_BASE, _HTTP_TIMEOUT
from src.metadata.errors import is_transient

if TYPE_CHECKING:
    from src.config import DiscogsConfig

log = logging.getLogger(__name__)

# STAB-4: absolute ceiling on the collection-index paging loop.  The loop's
# natural exits are an empty page or ``page >= pagination.pages``; a malformed or
# hostile pagination response (a huge ``pages`` value with never-empty pages) or a
# logic bug would otherwise page WITHOUT BOUND.  Under the documented systemd crash
# loop (Restart=on-failure / RestartSec=10, and StartLimitBurst added by this same
# fix) each restart re-pages from zero, so an unbounded build pins the appliance at
# the authenticated 60-request/minute rate limit.  At 100 releases/page this cap is
# 100,000 records — several times the most extreme personal vinyl collection, so it
# never clips a real one; it only bounds the pathological case.
_MAX_COLLECTION_PAGES = 1000

# #179: Discogs disambiguates same-named artists by appending " (2)", " (3)",
# etc. to the NAME field ("Nirvana (2)" is the UK 60s band).  The suffix is a
# Discogs rendering artifact, not part of the artist's name, so it is stripped
# from INDEX artist names before comparison.  Titles never carry it.
_ARTIST_DISAMBIG_RE = re.compile(r"\s*\(\d+\)\s*$")

# #179 tier 2 / #183: a single trailing parenthetical OR square-bracket
# qualifier — the dominant decoration divergences between Shazam's
# Apple-Music-backed catalogue and Discogs vinyl titles ("Rumours (Deluxe
# Edition)" / "Rumours [Deluxe Edition]" vs "Rumours"; brackets are a
# standard iTunes/Apple Music form).  Deliberately anchored and single:
# interior qualifiers are part of the title ("(What's the Story) Morning
# Glory?" is untouched), and STACKED decorations ("Pet Sounds (Mono)
# (Remastered)") remain a documented conservative miss (one strip only).
#
# The strip is applied ONE SIDE AT A TIME (stripped query vs raw index title,
# or raw query vs stripped index title) — never both sides in the same
# comparison.  A both-sides strip would equate two DIFFERENT albums that each
# carry a distinct trailing parenthetical ("Live (1975)" vs an owned "Live
# (1980)"), re-introducing the wrong-write-target class this fix exists to
# kill (caught by the #179 cold review; regression-pinned).
#
# ACCEPTED RESIDUAL: a decorated query can still wrongly credit an owned
# plain-titled member of a family distinguished only by parentheticals — e.g.
# Discogs titles every colour-era Weezer album "Weezer", so playing "Weezer
# (Blue Album)" while owning only the Green album credits Green.  Uniqueness
# protects only when 2+ owned members MATCH THE QUERY under the one-side
# strip; a plain-titled owned member alongside decorated siblings is still
# credited (the siblings' distinct parentheticals disqualify them, so
# ambiguity never arms).  The future
# hardening is a decoration-keyword allowlist for the query-side strip
# ("deluxe", "edition", "remaster(ed)", "expanded", "anniversary", …), tracked
# as a follow-up issue rather than smuggled into #179.
_TRAILING_PAREN_RE = re.compile(r"\s*(?:\([^()]*\)|\[[^\[\]]*\])\s*$")


# #179: NFKC does NOT fold typographic punctuation to ASCII (U+2019 "’" stays
# U+2019), and Shazam/Apple Music systematically use the typographic forms
# where Discogs contributors typically type ASCII.  Fold the common variants
# explicitly; everything here is a rendering choice, never a semantic one.
_PUNCT_FOLD = str.maketrans({
    "‘": "'", "’": "'", "ʼ": "'", "`": "'", "´": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...",
})


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


def _normalize_artist(s: str) -> str:
    """Normalise an artist name for collection matching (#223, via #183).

    On top of :func:`_normalize_term`: folds ``&`` to ``and`` and strips one
    leading ``the`` — the two real-world variant classes ("Rolling Stones" vs
    "The Rolling Stones", "Simon & Garfunkel" vs "Simon And Garfunkel") that
    the old ownership-only strategy 1 happened to bridge and exact equality
    silently lost.  ARTIST names only, never titles ("The Wall" must not
    equal "Wall"); anything fuzzier stays out per the exact-or-nothing
    principle.  Symmetric edge: "The The" folds to "the" on both sides.

    The ``&`` fold runs on BOTH sides of the NFKC inside ``_normalize_term``
    (second-pass catch): a fullwidth ``＆`` (U+FF06) only becomes ``&`` via
    NFKC, so a pre-normalize-only replace would miss it — the same ordering
    discipline ``_normalize_term``'s own punctuation fold documents.
    """
    s = _normalize_term(s.replace("&", " and "))
    s = " ".join(s.replace("&", " and ").split())
    if s.startswith("the "):
        s = s[4:]
    return s


def _normalize_term(s: str) -> str:
    """Normalise one side of a collection-match comparison (#179).

    The explicit table folds typographic punctuation NFKC leaves alone (curly
    quotes, en/em-dashes) and runs on BOTH sides of NFKC (#179 cold review +
    second pass): before, because NFKC decomposes some table inputs (``´``
    becomes space + combining acute) so a post-NFKC-only translate would never
    see them; and again after, because some characters' NFKC *outputs* are
    table keys (fullwidth grave U+FF40 folds to `````, ``ŉ`` to ``'n``).
    The table's ASCII outputs are NFKC-stable, so the second pass cannot
    regress the first.  casefold() is the aggressive Unicode lowercase, and
    interior whitespace is collapsed so spacing differences can't defeat an
    exact comparison.
    """
    s = unicodedata.normalize("NFKC", s.translate(_PUNCT_FOLD)).translate(_PUNCT_FOLD).casefold()
    return " ".join(s.split())


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

        # Lazily-built, session-cached index of the user's collection:
        #   {release_id: {"instance_id", "title", "artists"}}.
        # The collection is static within a session, so building this ONCE and
        # matching locally replaces the per-candidate N+1 membership GETs (P-1).
        self._collection_index: Optional[dict] = None

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

        Both strategies match against a session-cached in-memory index of the
        collection (built once, the collection being static within a session),
        so neither pays a per-candidate HTTP cost (P-1):

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
            return any(
                _normalize_artist(_ARTIST_DISAMBIG_RE.sub("", a)) == artist_key
                for a in entry["artists"]
            )

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
            if _normalize_term(entry["title"]) == album_key and entry_artist_matches(entry):
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
            if _normalize_term(entry["title"]) == album_key
            and entry_artist_matches(entry)
        ]

        # Tier 2: retry with a single trailing parenthetical stripped from ONE
        # side at a time — the decorated-query direction (stripped query vs raw
        # index title: "Rumours (Deluxe Edition)" → owned "Rumours") and the
        # decorated-index direction (raw query vs stripped index title:
        # "The Wall" → owned "The Wall (UK)").  Never stripped-vs-stripped,
        # which would equate two different parenthetical siblings (see the
        # _TRAILING_PAREN_RE comment).  Only reached when tier 1 found nothing.
        if not matches:
            album_key_stripped = _normalize_term(_TRAILING_PAREN_RE.sub("", album))
            if album_key_stripped:
                def tier2_album_matches(index_title: str) -> bool:
                    title_key = _normalize_term(index_title)
                    if album_key_stripped == title_key:
                        return True
                    title_key_stripped = _normalize_term(
                        _TRAILING_PAREN_RE.sub("", index_title)
                    )
                    return bool(title_key_stripped) and album_key == title_key_stripped

                matches = [
                    (release_id, entry)
                    for release_id, entry in index.items()
                    if tier2_album_matches(entry["title"])
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
            entries = []
            for track in release.tracklist:
                # Headings have type_ == 'heading' and typically no position
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
        except Exception as e:
            _reraise_if_transient(e)
            log.warning(f"Failed to fetch tracklist for release {release_id}: {e}")
            return []

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
        results = self._client.search(album, artist=artist, type="release")
        return list(results.page(1)[:limit])

    def _get_collection_index(self) -> dict:
        """Build (once per session) and return an in-memory index of the user's
        collection: ``{release_id: {"instance_id", "title", "artists"}}``.

        Replaces the old per-candidate membership GET (one per database
        candidate, up to 25) and the full re-walk with a single paginated fetch
        + local lookups (P-1).  The collection is static within a session and
        the process restarts daily, so there is no TTL.

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
        if self._collection_index is not None:
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
        log.debug(f"Built collection index: {len(index)} release(s).")
        return index

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

        # Tracklist — fetch separately; log but don't fail on error
        tracklist = self.get_tracklist(release.id)

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
