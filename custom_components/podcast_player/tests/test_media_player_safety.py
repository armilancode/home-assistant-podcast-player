"""Regression tests for safe media-player target handling."""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.podcast_player.coordinator import PodcastUpdateCoordinator
from custom_components.podcast_player.storage import default_data


class FakeStates:
    """Minimal hass.states replacement."""

    def __init__(self, state: object | None) -> None:
        self._state = state

    def get(self, entity_id: str) -> object | None:
        """Return configured fake state."""
        return self._state


class FakeServices:
    """Minimal hass.services replacement."""

    def __init__(self) -> None:
        self.called = False

    async def async_call(self, *args, **kwargs) -> None:
        """Record service calls."""
        self.called = True


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


def _coordinator(state: object | None) -> PodcastUpdateCoordinator:
    coord = PodcastUpdateCoordinator.__new__(PodcastUpdateCoordinator)
    coord.storage = FakeStorage()
    coord.hass = SimpleNamespace(states=FakeStates(state), services=FakeServices())
    coord.async_set_updated_data = lambda data: None
    return coord


def test_playback_target_off_fails_before_service_call() -> None:
    """An off target must fail before Podcast Player calls play_media."""
    state = SimpleNamespace(state="off", attributes={}, name="Bedroom TV")
    coord = _coordinator(state)

    with pytest.raises(HomeAssistantError):
        coord._validate_media_player_output_target("media_player.bedroom_tv")

    assert not coord.hass.services.called


def test_stop_unavailable_active_target_clears_without_service_call() -> None:
    """Stopping an unavailable active target clears local state without direct internals."""
    state = SimpleNamespace(state="unavailable", attributes={}, name="Bedroom TV")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.bedroom_tv"

    asyncio.run(coord.async_stop_media_player("media_player.bedroom_tv"))

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.saved
    assert not coord.hass.services.called
