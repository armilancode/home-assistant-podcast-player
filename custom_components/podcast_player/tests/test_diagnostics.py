"""Tests for Podcast Player diagnostics."""

import json
from types import SimpleNamespace

from homeassistant.components.diagnostics import REDACTED
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.podcast_player.const import (
    CONF_INITIAL_RSS_URL,
    CONF_NEW_FEED_URL,
    CONF_REMOVE_FEED_ID,
    DOMAIN,
)
from custom_components.podcast_player.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.podcast_player.storage import PodcastStorage, default_data


async def test_config_entry_diagnostics_redacts_private_data(hass) -> None:
    """Diagnostics remain useful without exposing subscriptions or playback data."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()
    storage.data["settings"]["speaker_proxy_secret"] = "private-proxy-secret"
    storage.data["feeds"]["private-feed-id"] = {
        "feed_id": "private-feed-id",
        "rss_url": "https://private.example.test/feed.xml",
        "title": "Private Feed Title",
        "enabled": True,
        "status": "failed",
    }
    storage.data["episodes"]["private-episode-id"] = {
        "episode_id": "private-episode-id",
        "feed_id": "private-feed-id",
        "audio_url": "https://private.example.test/episode.mp3",
        "title": "Private Episode Title",
    }
    storage.data["progress"]["private-episode-id"] = {
        "played": False,
        "position": 42,
    }
    player = storage.data["player"]
    player.update(
        {
            "state": "playing",
            "current_episode_id": "private-episode-id",
            "target_media_player": "media_player.private_room",
            "target_media_player_name": "Private Room",
            "speaker_last_error": "Private playback error",
        }
    )
    player["external_session"].update(
        {
            "active": True,
            "episode_id": "private-episode-id",
            "target_media_player": "media_player.private_room",
            "transport_state": "PLAYING",
            "supported_actions": ["pause", "stop"],
            "control_source": "dlna",
            "progress_source": "device",
            "media_matches_session": True,
            "last_error": "Private transport error",
        }
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INITIAL_RSS_URL: "https://private.example.test/initial.xml"},
        options={
            CONF_NEW_FEED_URL: "https://private.example.test/new.xml",
            CONF_REMOVE_FEED_ID: "private-feed-id",
            "refresh_interval_minutes": 60,
        },
    )
    entry.runtime_data = SimpleNamespace(
        storage=storage,
        coordinator=SimpleNamespace(
            last_update_success=False,
            last_exception=RuntimeError("Private refresh error"),
        ),
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["config_entry"]["data"][CONF_INITIAL_RSS_URL] == REDACTED
    assert result["config_entry"]["options"][CONF_NEW_FEED_URL] == REDACTED
    assert result["config_entry"]["options"][CONF_REMOVE_FEED_ID] == REDACTED
    assert result["storage"]["settings"]["speaker_proxy_secret"] == REDACTED
    assert result["storage"]["library_counts"] == {
        "total_feeds": 1,
        "enabled_feeds": 1,
        "failed_feeds": 1,
        "total_episodes": 1,
        "unplayed": 1,
        "partially_played": 1,
    }
    assert result["coordinator"] == {
        "last_update_success": False,
        "has_update_error": True,
    }
    assert result["playback"]["has_current_episode"] is True
    assert result["playback"]["has_output_target"] is True
    assert result["playback"]["has_playback_error"] is True
    assert result["playback"]["external_session"] == {
        "active": True,
        "transport_state": "PLAYING",
        "supported_actions": ["pause", "stop"],
        "control_source": "dlna",
        "progress_source": "device",
        "media_matches_session": True,
        "has_error": True,
    }

    serialized = json.dumps(result)
    for private_value in (
        "private.example.test",
        "private-proxy-secret",
        "private-feed-id",
        "private-episode-id",
        "Private Feed Title",
        "Private Episode Title",
        "media_player.private_room",
        "Private Room",
        "Private playback error",
        "Private transport error",
        "Private refresh error",
    ):
        assert private_value not in serialized
