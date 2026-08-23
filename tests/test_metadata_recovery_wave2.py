"""Cross-layer acceptance evidence for Wave 2 metadata recovery (#420/#421).

These tests deliberately join the real resolver, tracker, and writer behavior.
Only Discogs' collection-search/rebuild reader seam and HTTP transport seam are
substituted, so the matrix protects the handoff between cache truth, definitive
writer evidence, and the one safe absolute credit.
"""

from unittest.mock import MagicMock

import pytest

from src.audio.recognizer import RawRecognitionResult
from src.metadata.discogs.outcomes import CollectionRefreshResult, CollectionRefreshState
from src.metadata.models import MetadataSource, PlaySession
from src.metadata.resolver import MetadataResolver
from src.tracking.listen_tracker import ListenTracker
from tests.factories import make_discogs_config, make_discogs_http, make_discogs_writer


_KEY = ("sonic youth", "sister")
_STALE_RELEASE_ID = 999
_STALE_INSTANCE_ID = 77
_PLAY_COUNT_FIELD_ID = 3


def _response(body, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body
    return response


def _complete_snapshot(instances):
    """Return the strict one-page writer snapshot used for missing proof."""
    return {
        "pagination": {
            "page": 1,
            "pages": 1,
            "items": len(instances),
            "per_page": max(1, len(instances)),
        },
        "releases": instances,
    }


class _ReaderSeam:
    """Small synchronous reader seam driven through the resolver's real gate."""

    def __init__(self, *, rebuild_result=None):
        self.rebuild_result = rebuild_result
        self.refresh_attempts = 0
        self.page_walks = 0
        self.rebuild_calls = 0

    async def run(self, fn, *args):
        return fn(*args)

    def search_collection(self, _artist, _album):
        return None

    def search_database(self, _artist, _album):
        return {
            "album": "Sister",
            "release_id": _STALE_RELEASE_ID,
            "instance_id": None,
            "tracklist": [],
            "genres": [],
        }

    def refresh_index_and_research(self, _artist, _album):
        self.refresh_attempts += 1
        if self.refresh_attempts == 1:
            self.page_walks += 1
            raise ConnectionError("simulated collection page failure")
        return CollectionRefreshResult(
            CollectionRefreshState.COOLDOWN_SKIPPED,
            cooldown_follows_successful_rebuild=False,
        )

    def rebuild_collection_and_research(self, _artist, _album):
        self.rebuild_calls += 1
        return self.rebuild_result


def _resolver(reader):
    resolver = MetadataResolver(reader, coverart=MagicMock())
    resolver.coverart.get_cover_art_url.return_value = None
    return resolver


def _session():
    session = PlaySession()
    session.album_release_id = _STALE_RELEASE_ID
    session.album_instance_id = _STALE_INSTANCE_ID
    session.album_resolve_key = _KEY
    return session


def _writer_with_http_responses(*responses):
    """Build the real writer with a deterministic, no-network HTTP seam."""
    http = make_discogs_http()
    writer = make_discogs_writer(
        http=http,
        config=make_discogs_config(last_played_field_name=None),
    )
    writer._collection_fields = {"Play Count": _PLAY_COUNT_FIELD_ID}
    gets = []
    posts = []
    pending = iter(responses)

    def fake_get(url, **_kwargs):
        gets.append(url)
        return next(pending)

    def fake_post(url, **kwargs):
        posts.append((url, kwargs["json"]))
        return _response({}, status_code=204)

    http.session.get = fake_get
    http.session.post = fake_post
    return writer, gets, posts


def _stale_cache_payload(instance_id=_STALE_INSTANCE_ID):
    return {
        "album": "Sister",
        "release_id": _STALE_RELEASE_ID,
        "instance_id": instance_id,
        "tracklist": [],
        "genres": [],
    }


@pytest.mark.asyncio
async def test_failed_refresh_then_cooldown_skip_returns_database_uncached():
    """#420: failed refresh provenance forbids a false database downgrade cache."""
    reader = _ReaderSeam()
    resolver = _resolver(reader)
    raw = RawRecognitionResult("Schizophrenia", "Sonic Youth", "Sister")

    first = await resolver.resolve(raw)
    second = await resolver.resolve(raw)

    assert (first.source, second.source) == (
        MetadataSource.DISCOGS_DATABASE,
        MetadataSource.DISCOGS_DATABASE,
    )
    assert len(resolver._album_cache) == 0
    assert reader.page_walks == 1
    assert reader.refresh_attempts == 2


@pytest.mark.asyncio
async def test_stale_instance_recovers_to_safe_replacement_and_credits_once():
    """#421: a strict MISSING tuple permits one same-release replacement credit."""
    reader = _ReaderSeam(rebuild_result=_stale_cache_payload(instance_id=88))
    resolver = _resolver(reader)
    resolver._cache_store(_KEY, MetadataSource.DISCOGS_COLLECTION, _stale_cache_payload())
    writer, gets, posts = _writer_with_http_responses(
        _response(_complete_snapshot([
            {
                "instance_id": 88,
                "folder_id": 0,
                "basic_information": {"id": _STALE_RELEASE_ID},
                "notes": [],
            },
        ])),
        _response({"releases": [
            {
                "instance_id": 88,
                "notes": [{"field_id": _PLAY_COUNT_FIELD_ID, "value": "5"}],
            },
        ]}),
    )
    tracker = ListenTracker(writer, recover_collection_instance=resolver.recover_collection_instance)
    session = _session()

    await tracker._credit_completed_album(session)

    assert reader.rebuild_calls == 1
    assert len(gets) == 2
    assert len(posts) == 1
    post_url, post_body = posts[0]
    assert post_url.endswith(
        "/collection/folders/0/releases/999/instances/88/fields/3"
    )
    assert post_body == {"value": "6"}
    assert (session.album_release_id, session.album_instance_id) == (_STALE_RELEASE_ID, 88)
    assert session.credited is True
    cached = resolver._cache_get(_KEY)
    assert cached is not None
    assert cached[0] is MetadataSource.DISCOGS_COLLECTION
    assert cached[1]["instance_id"] == 88


@pytest.mark.asyncio
async def test_duplicate_replacement_instances_never_select_or_cache_a_write_target():
    """#421: multiplicity is a refusal, not permission to choose the first copy."""
    reader = _ReaderSeam(rebuild_result=_stale_cache_payload(instance_id=88))
    resolver = _resolver(reader)
    resolver._cache_store(_KEY, MetadataSource.DISCOGS_COLLECTION, _stale_cache_payload())
    writer, gets, posts = _writer_with_http_responses(
        _response(_complete_snapshot([
            {
                "instance_id": 88,
                "folder_id": 0,
                "basic_information": {"id": _STALE_RELEASE_ID},
                "notes": [],
            },
            {
                "instance_id": 89,
                "folder_id": 0,
                "basic_information": {"id": _STALE_RELEASE_ID},
                "notes": [],
            },
        ])),
    )
    tracker = ListenTracker(writer, recover_collection_instance=resolver.recover_collection_instance)

    await tracker._credit_completed_album(_session())

    assert len(gets) == 1
    assert reader.rebuild_calls == 0
    assert posts == []
    assert resolver._cache_get(_KEY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_instance",
    [
        {"instance_id": 88, "basic_information": {"id": _STALE_RELEASE_ID}},
        {"instance_id": 88, "folder_id": 0},
        {"instance_id": "88", "folder_id": 0, "basic_information": {"id": _STALE_RELEASE_ID}},
        {"instance_id": 0, "folder_id": 0, "basic_information": {"id": _STALE_RELEASE_ID}},
        {"instance_id": -88, "folder_id": 0, "basic_information": {"id": _STALE_RELEASE_ID}},
    ],
    ids=["missing-folder", "missing-basic-information", "string-id", "zero-id", "negative-id"],
)
async def test_malformed_missing_snapshot_never_escapes_to_recovery_or_post(malformed_instance):
    """Deferred Task 2 shapes remain ABORTs through the complete tracker path."""
    reader = _ReaderSeam(rebuild_result=_stale_cache_payload(instance_id=88))
    resolver = _resolver(reader)
    resolver._cache_store(_KEY, MetadataSource.DISCOGS_COLLECTION, _stale_cache_payload())
    writer, _gets, posts = _writer_with_http_responses(
        _response(_complete_snapshot([malformed_instance])),
    )
    tracker = ListenTracker(writer, recover_collection_instance=resolver.recover_collection_instance)

    await tracker._credit_completed_album(_session())

    assert reader.rebuild_calls == 0
    assert posts == []
    cached = resolver._cache_get(_KEY)
    assert cached is not None
    assert cached[1]["instance_id"] == _STALE_INSTANCE_ID
