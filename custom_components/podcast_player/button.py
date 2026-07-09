"""Button entities for HA Podcast Player."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import PodcastRuntime, PodcastUpdateCoordinator
from .entity import podcast_player_device_info

PARALLEL_UPDATES = 1


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up buttons."""
    runtime: PodcastRuntime = entry.runtime_data
    coordinator = runtime.coordinator
    async_add_entities(
        [
            RefreshButton(coordinator),
            PlayLatestButton(coordinator),
            PlayNextUnplayedButton(coordinator),
            MarkCurrentPlayedButton(coordinator),
        ]
    )


class PodcastButton(CoordinatorEntity[PodcastUpdateCoordinator], ButtonEntity):
    """Base button."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PodcastUpdateCoordinator) -> None:
        """Initialize button."""
        super().__init__(coordinator)
        self._attr_device_info = podcast_player_device_info()


class RefreshButton(PodcastButton):
    """Refresh feeds button."""

    _attr_name = "Refresh feeds"
    _attr_unique_id = "podcast_player_refresh"
    _attr_icon = "mdi:refresh"

    async def async_press(self) -> None:
        """Refresh feeds."""
        await self.coordinator.async_refresh_feeds()


class PlayLatestButton(PodcastButton):
    """Play latest episode button."""

    _attr_name = "Play latest episode"
    _attr_unique_id = "podcast_player_play_latest"
    _attr_icon = "mdi:play-circle-outline"
    _attr_entity_registry_enabled_default = False

    async def async_press(self) -> None:
        """Select/play latest episode in podcast state."""
        await self.coordinator.async_play_latest()


class PlayNextUnplayedButton(PodcastButton):
    """Play next unplayed episode button."""

    _attr_name = "Play next unplayed"
    _attr_unique_id = "podcast_player_play_next_unplayed"
    _attr_icon = "mdi:playlist-play"
    _attr_entity_registry_enabled_default = False

    async def async_press(self) -> None:
        """Select/play next unplayed episode in podcast state."""
        await self.coordinator.async_play_next_unplayed()


class MarkCurrentPlayedButton(PodcastButton):
    """Mark current episode played button."""

    _attr_name = "Mark current played"
    _attr_unique_id = "podcast_player_mark_current_played"
    _attr_icon = "mdi:check-circle-outline"
    _attr_entity_registry_enabled_default = False

    async def async_press(self) -> None:
        """Mark current episode played."""
        await self.coordinator.async_mark_current_played(True)
