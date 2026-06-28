"""Regression tests for safe media-player target handling."""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.podcast_player.const import PLAYER_ENTITY_ID
from custom_components.podcast_player.coordinator import PodcastUpdateCoordinator
from custom_components.podcast_player.storage import default_data


class FakeStates:
    """Minimal hass.states replacement."""

    def __init__(self, state: object | dict[str, object | None] | None) -> None:
        self._state = state

    def get(self, entity_id: str) -> object | None:
        """Return configured fake state."""
        if isinstance(self._state, dict):
            return self._state.get(entity_id)
        return self._state


class FakeServices:
    """Minimal hass.services replacement."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    @property
    def called(self) -> bool:
        """Return true if any service was called."""
        return bool(self.calls)

    async def async_call(self, *args, **kwargs) -> None:
        """Record service calls."""
        self.calls.append((args, kwargs))


class FakeStorage:
    """Minimal storage object."""

    def __init__(self) -> None:
        self.data = default_data()
        self.saved = False

    async def async_save(self) -> None:
        """Record saves."""
        self.saved = True

    def snapshot(self) -> dict:
        """Return current fake data."""
        return self.data

    def set_player_state(self, state: str, episode_id: str | None = None) -> None:
        """Set fake player state."""
        self.data["player"]["state"] = state
        if episode_id is not None:
            self.data["player"]["current_episode_id"] = episode_id

    def get_progress(self, episode_id: str) -> dict:
        """Return fake progress."""
        return self.data["progress"].setdefault(
            episode_id,
            {"episode_id": episode_id, "played": False, "position": 0, "duration": None, "last_played_at": None},
        )

    def save_progress(self, episode_id: str, position: float, duration=None, playing=None, speed=None) -> dict:
        """Save fake progress."""
        progress = self.get_progress(episode_id)
        progress["position"] = position
        progress["playing"] = playing
        return progress


def _coordinator(state: object | None) -> PodcastUpdateCoordinator:
    coord = PodcastUpdateCoordinator.__new__(PodcastUpdateCoordinator)
    coord.storage = FakeStorage()
    coord.hass = SimpleNamespace(states=FakeStates(state), services=FakeServices())
    coord.async_set_updated_data = lambda data: None
    return coord


def test_playback_target_off_fails_before_service_call() -> None:
    """An off target must fail before Podcast Player calls play_media."""
    state = SimpleNamespace(state="off", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)

    with pytest.raises(HomeAssistantError):
        coord._validate_media_player_output_target("media_player.kitchen_speaker")

    assert not coord.hass.services.called


def test_self_media_player_target_fails_before_service_call() -> None:
    """Podcast Player must never send media services to its own status entity."""
    state = SimpleNamespace(state="idle", attributes={}, name="Podcast Player")
    coord = _coordinator(state)

    with pytest.raises(HomeAssistantError):
        coord._validate_media_player_output_target(PLAYER_ENTITY_ID)

    assert not coord.hass.services.called


def test_playback_target_without_play_media_feature_fails_before_service_call() -> None:
    """A target that advertises features without PLAY_MEDIA must fail early."""
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    play_media = int(MediaPlayerEntityFeature.PLAY_MEDIA)
    unsupported_features = 1
    while unsupported_features & play_media:
        unsupported_features <<= 1
    state = SimpleNamespace(state="idle", attributes={"supported_features": unsupported_features}, name="Limited Player")
    coord = _coordinator(state)

    with pytest.raises(HomeAssistantError):
        coord._validate_media_player_output_target("media_player.limited")

    assert not coord.hass.services.called


def test_stop_unavailable_active_target_clears_without_service_call() -> None:
    """Stopping an unavailable active target clears local state without direct internals."""
    state = SimpleNamespace(state="unavailable", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    asyncio.run(coord.async_stop_media_player("media_player.kitchen_speaker"))

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.saved
    assert not coord.hass.services.called


def test_stop_missing_inactive_target_fails_without_service_call() -> None:
    """Stopping a missing last target must not call media_stop."""
    coord = _coordinator(None)
    coord.storage.data["player"]["last_target_media_player"] = "media_player.old_speaker"

    with pytest.raises(HomeAssistantError):
        asyncio.run(coord.async_stop_media_player())

    assert coord.storage.saved
    assert coord.storage.data["player"]["speaker_last_error"]
    assert not coord.hass.services.called


def test_pause_off_active_target_clears_without_service_call() -> None:
    """Pausing an off active target clears stale speaker state without media_pause."""
    state = SimpleNamespace(state="off", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    with pytest.raises(HomeAssistantError):
        asyncio.run(coord.async_pause())

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.data["player"]["speaker_last_error"]
    assert not coord.hass.services.called


def test_resume_off_active_target_clears_without_service_call() -> None:
    """Resuming an off active target clears stale speaker state without media_play."""
    state = SimpleNamespace(state="off", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "paused"
    coord.storage.data["player"]["current_episode_id"] = "episode-1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    with pytest.raises(HomeAssistantError):
        asyncio.run(coord.async_resume())

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.data["player"]["speaker_last_error"]
    assert not coord.hass.services.called


def test_seek_off_active_target_saves_progress_without_service_call() -> None:
    """Seeking while the active target is off saves progress and avoids media_seek."""
    state = SimpleNamespace(state="off", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["current_episode_id"] = "episode-1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    asyncio.run(coord.async_seek("episode-1", 42))

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.data["progress"]["episode-1"]["position"] == 42
    assert not coord.hass.services.called
