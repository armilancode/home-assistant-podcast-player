"""Tests for Podcast Player media source helpers."""

from custom_components.podcast_player.const import DOMAIN
from custom_components.podcast_player.media_source import _parts, media_source_id_for_episode


def test_media_source_identifier_parts() -> None:
    """Media source identifiers are normalized into path parts."""
    assert _parts(None) == []
    assert _parts("") == []
    assert _parts("/feed/feed_123/latest/") == ["feed", "feed_123", "latest"]


def test_media_source_id_for_episode() -> None:
    """Episode media source IDs use the integration domain."""
    assert media_source_id_for_episode("ep_123") == f"media-source://{DOMAIN}/episode/ep_123"
