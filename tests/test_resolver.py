"""Tests for MetadataResolver fallback chain."""

import pytest
from unittest.mock import MagicMock

from src.metadata.models import MetadataSource
from src.audio.recognizer import RawRecognitionResult


RAW = RawRecognitionResult(
    title="So What",
    artist="Miles Davis",
    album="Kind of Blue",
)

DISCOGS_RESULT = {
    "album": "Kind of Blue",
    "year": "1959",
    "label": "Columbia",
    "catalog_number": "CL 1355",
    "release_id": 12345,
    "instance_id": 99999,
    "cover_art_url": "https://example.com/kob.jpg",
    "tracklist": [],
}


class TestMetadataResolverFallbackChain:
    def _make_resolver(self):
        from src.metadata.resolver import MetadataResolver
        config = {
            "discogs": {
                "user_token": "x",
                "username": "u",
                "listened_field_name": "Listened to?",
                "listened_field_value": "Yes",
            }
        }
        return MetadataResolver(config)

    @pytest.mark.asyncio
    async def test_returns_collection_result_when_found(self):
        resolver = self._make_resolver()
        resolver.discogs.search_collection = MagicMock(return_value=DISCOGS_RESULT)
        result = await resolver.resolve(RAW)
        assert result.source == MetadataSource.DISCOGS_COLLECTION
        assert result.year == "1959"
        assert result.discogs_release_id == 12345

    @pytest.mark.asyncio
    async def test_falls_back_to_database_when_not_in_collection(self):
        resolver = self._make_resolver()
        resolver.discogs.search_collection = MagicMock(return_value=None)
        resolver.discogs.search_database = MagicMock(return_value=DISCOGS_RESULT)
        result = await resolver.resolve(RAW)
        assert result.source == MetadataSource.DISCOGS_DATABASE

    @pytest.mark.asyncio
    async def test_falls_back_to_musicbrainz_when_discogs_misses(self):
        resolver = self._make_resolver()
        resolver.discogs.search_collection = MagicMock(return_value=None)
        resolver.discogs.search_database = MagicMock(return_value=None)
        resolver.coverart.get_cover_art_url = MagicMock(return_value="https://mb.org/art.jpg")
        result = await resolver.resolve(RAW)
        assert result.source == MetadataSource.FALLBACK
        assert result.cover_art_url == "https://mb.org/art.jpg"
