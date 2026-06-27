"""Tests for Podcast Player websocket/API payload helpers."""

from custom_components.podcast_player.api import _public_episode
from custom_components.podcast_player.const import DOMAIN


def test_public_episode_includes_media_source_id() -> None:
    """Public episode payloads expose the native HA media-source URI."""
    payload = _public_episode(
        {
            "episode_id": "ep_123",
            "feed_id": "feed_1",
            "title": "Episode",
            "audio_url": "https://example.test/episode.mp3",
        },
        progress={"position": 12, "played": False},
        feed={"title": "Feed One"},
    )

    assert payload["media_source_id"] == f"media-source://{DOMAIN}/episode/ep_123"
    assert payload["audio_url"] == "https://example.test/episode.mp3"
    assert payload["proxy_url"] == "/api/podcast_player/proxy/ep_123"
