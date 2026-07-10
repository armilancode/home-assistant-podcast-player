"""Tests for service action failure behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.podcast_player import async_register_services
from custom_components.podcast_player.const import (
    DOMAIN,
    SERVICE_MARK_FEED_PLAYED,
    SERVICE_REFRESH,
    SERVICE_REMOVE_FEED,
)
from custom_components.podcast_player.coordinator import PodcastUpdateCoordinator
from custom_components.podcast_player.storage import default_data


def _install_runtime(hass, coordinator: SimpleNamespace, storage: SimpleNamespace | None = None) -> None:
    """Install a minimal runtime for service action tests."""
    runtime = SimpleNamespace(
        coordinator=coordinator,
        storage=storage or SimpleNamespace(get_feed=lambda feed_id: None),
    )
    hass.data.setdefault(DOMAIN, {})["entry"] = runtime


def _coordinator() -> PodcastUpdateCoordinator:
    """Return a coordinator shell with real validation methods and fake storage."""
    storage = SimpleNamespace()
    storage.data = default_data()
    storage.async_save = AsyncMock()
    storage.snapshot = lambda: storage.data
    storage.get_episode = lambda episode_id: storage.data["episodes"].get(episode_id)
    storage.get_progress = lambda episode_id: storage.data["progress"].setdefault(
        episode_id,
        {"episode_id": episode_id, "played": False, "position": 0, "duration": None},
    )
    storage.save_progress = lambda episode_id, position, duration=None, playing=None, speed=None: storage.get_progress(
        episode_id
    )
    storage.mark_played = lambda episode_id, played: storage.get_progress(episode_id) | {"played": played}
    storage.set_speed = lambda speed, episode_id=None: None

    coord = PodcastUpdateCoordinator.__new__(PodcastUpdateCoordinator)
    coord.storage = storage
    coord.async_set_updated_data = lambda data: None
    return coord


async def test_remove_feed_missing_raises_service_validation_error(hass) -> None:
    """Removing an unknown feed must not silently succeed."""
    async_register_services(hass)
    coordinator = SimpleNamespace(async_remove_feed=AsyncMock(return_value=False))
    _install_runtime(hass, coordinator)

    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REMOVE_FEED,
            {"feed_id": "missing"},
            blocking=True,
        )

    assert error.value.translation_key == "feed_not_found"
    assert error.value.translation_placeholders == {"feed_id": "missing"}
    coordinator.async_remove_feed.assert_awaited_once_with("missing", True)


async def test_feed_action_rejects_media_player_target(hass) -> None:
    """Feed-targeting actions should guide users to media_player_entity_id."""
    async_register_services(hass)
    coordinator = SimpleNamespace(hass=hass)
    _install_runtime(hass, coordinator)

    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH,
            {"entity_id": "media_player.kitchen_speaker"},
            blocking=True,
        )

    assert error.value.translation_key == "media_player_target_field"
    assert error.value.translation_placeholders == {"service": "refresh_feeds"}


async def test_mark_feed_played_requires_feed_selection(hass) -> None:
    """Marking a feed requires a selected feed target or feed identifier."""
    async_register_services(hass)
    coordinator = SimpleNamespace(hass=hass, async_mark_feed_played=AsyncMock())
    _install_runtime(hass, coordinator)

    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_MARK_FEED_PLAYED,
            {},
            blocking=True,
        )

    assert error.value.translation_key == "feed_selection_required"
    assert error.value.translation_placeholders is None
    coordinator.async_mark_feed_played.assert_not_awaited()


async def test_unknown_episode_actions_raise_service_validation_error() -> None:
    """Episode-id actions reject unknown episode IDs instead of creating orphan progress."""
    coordinator = _coordinator()

    for action in (
        lambda: coordinator.async_play_episode("missing"),
        lambda: coordinator.async_save_progress("missing", 12),
        lambda: coordinator.async_mark_played("missing", True),
        lambda: coordinator.async_set_speed(1.25, "missing"),
    ):
        with pytest.raises(ServiceValidationError) as error:
            await action()
        assert error.value.translation_key == "episode_not_found"
        assert error.value.translation_placeholders == {"episode_id": "missing"}


async def test_mark_current_without_current_episode_raises_home_assistant_error() -> None:
    """State-dependent actions should report that the requested action cannot run."""
    coordinator = _coordinator()

    with pytest.raises(HomeAssistantError) as error:
        await coordinator.async_mark_current_played(True)

    assert error.value.translation_key == "no_current_episode_to_mark"


async def test_stop_media_player_without_known_target_raises_home_assistant_error() -> None:
    """Stopping a media-player target requires an explicit or remembered target."""
    coordinator = _coordinator()

    with pytest.raises(HomeAssistantError) as error:
        await coordinator.async_stop_media_player()

    assert error.value.translation_key == "no_media_player_target"
