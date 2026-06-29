"""Tests for Podcast Player websocket/API payload helpers."""

from types import SimpleNamespace

from custom_components.podcast_player.api import _public_episode, _public_output_targets, _public_settings
from custom_components.podcast_player.const import DOMAIN, PLAYER_ENTITY_ID


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


def test_public_settings_hide_proxy_secret() -> None:
    """Frontend settings must not expose internal proxy secrets."""
    payload = _public_settings(
        {
            "default_playback_speed": 1.0,
            "enhanced_dlna_controls": True,
            "speaker_proxy_secret": "secret-value",
        }
    )

    assert payload == {
        "default_playback_speed": 1.0,
        "enhanced_dlna_controls": True,
    }


class FakeStates:
    """Minimal hass.states replacement for output target payload tests."""

    def __init__(self, states: list[SimpleNamespace]) -> None:
        self._states = states

    def async_all(self, domain: str) -> list[SimpleNamespace]:
        """Return configured fake states for one domain."""
        return self._states if domain == "media_player" else []


def _media_state(entity_id: str, state: str, name: str, features: int = 0) -> SimpleNamespace:
    """Build a minimal HA state object."""
    return SimpleNamespace(
        entity_id=entity_id,
        state=state,
        name=name,
        attributes={"friendly_name": name, "supported_features": features},
    )


def test_public_output_targets_exposes_target_statuses() -> None:
    """Output target payloads include frontend-ready target status information."""
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    play_media = int(MediaPlayerEntityFeature.PLAY_MEDIA)
    unsupported_features = 1
    while unsupported_features & play_media:
        unsupported_features <<= 1

    hass = SimpleNamespace(
        states=FakeStates(
            [
                _media_state(PLAYER_ENTITY_ID, "idle", "Podcast Player"),
                _media_state("media_player.ready_speaker", "idle", "Ready Speaker", play_media),
                _media_state("media_player.off_speaker", "off", "Off Speaker", play_media),
                _media_state("media_player.missing_speaker", "unavailable", "Missing Speaker", play_media),
                _media_state("media_player.limited_speaker", "idle", "Limited Speaker", unsupported_features),
                _media_state("media_player.spotify", "playing", "Spotify", play_media),
            ]
        )
    )

    targets = {target["entity_id"]: target for target in _public_output_targets(hass)}

    assert set(targets) == {
        "media_player.ready_speaker",
        "media_player.off_speaker",
        "media_player.missing_speaker",
        "media_player.limited_speaker",
    }
    assert targets["media_player.ready_speaker"]["status"] == "ready"
    assert targets["media_player.ready_speaker"]["playable"] is True
    assert targets["media_player.off_speaker"]["status"] == "off"
    assert targets["media_player.off_speaker"]["playable"] is False
    assert targets["media_player.missing_speaker"]["status"] == "unavailable"
    assert targets["media_player.missing_speaker"]["playable"] is False
    assert targets["media_player.limited_speaker"]["status"] == "unsupported"
    assert targets["media_player.limited_speaker"]["playable"] is False
